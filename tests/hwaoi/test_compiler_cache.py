"""Stage reuse, repair, cancelled-build locks and preservation of local game state."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from compiler import backend, cache


class CompilerCacheTests(unittest.TestCase):
    def put(self, root, relative, data=b"owned fixture"):
        p = root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return cache.digest(p)

    def test_sync_keeps_mtimes_backs_up_changes_and_removes_only_managed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); bundle = root / "bundle"; out = root / "output"
            files = {"source/runtime/a.c": self.put(bundle, "source/runtime/a.c", b"first")}
            dest = lambda p: Path(p).relative_to("source")
            cache.synchronize(bundle, out, files, dest, Path("work/backups/one"))
            original = (out / "runtime/a.c").stat().st_mtime_ns
            cache.synchronize(bundle, out, files, dest, Path("work/backups/two"))
            self.assertEqual((out / "runtime/a.c").stat().st_mtime_ns, original)
            self.assertFalse((out / "work/backups/two").exists())
            previous = dict(files)
            files["source/runtime/a.c"] = self.put(bundle, "source/runtime/a.c", b"second")
            cache.synchronize(bundle, out, files, dest, Path("work/backups/three"), previous)
            self.assertEqual((out / "work/backups/three/runtime/a.c").read_bytes(), b"first")
            self.put(out, "work/saves/DATA.BIN", b"keep save")
            cache.synchronize(bundle, out, {}, dest, Path("work/backups/four"), files)
            self.assertFalse((out / "runtime/a.c").exists())
            self.assertEqual((out / "work/saves/DATA.BIN").read_bytes(), b"keep save")
            with self.assertRaises(ValueError):
                cache.inside(out, "../escape")
            # Deferred bundle inputs still fail their hash check before replacing a working file.
            with self.assertRaisesRegex(ValueError, "hash check"):
                cache.synchronize(bundle, out, {"source/runtime/a.c": "0" * 64}, dest, Path("work/backups/bad"))

    def test_stable_tool_paths_and_identity_change_on_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); bundle = root / "bundle"; out = root / "output"
            files = {p: self.put(bundle, p) for p in ("llvm/bin/clang.exe", "deps/include/a.h", "bin/ninja.exe")}
            stamp = cache.prepare_tools(bundle, out, {"files": files}, Path("work/backups/one"))
            first = stamp.read_bytes(); mtime = stamp.stat().st_mtime_ns
            cache.prepare_tools(bundle, out, {"files": files}, Path("work/backups/two"))
            self.assertEqual(stamp.stat().st_mtime_ns, mtime)
            (out / "work/toolchain/llvm/bin/clang.exe").write_bytes(b"corruption")
            cache.prepare_tools(bundle, out, {"files": files}, Path("work/backups/three"))
            self.assertNotEqual(stamp.read_bytes(), first)
            self.assertEqual((out / "work/toolchain/llvm/bin/clang.exe").read_bytes(), b"owned fixture")

    def test_resource_cache_rechecks_changed_bytes_and_rejects_wrong_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); folder = root / "cache"; nup = root / "source.nup"
            nup.write_bytes(b"owned source"); self.put(folder, "cache.bin", b"ABCD")
            identity = {"header": "h", "toc": "t", "size": nup.stat().st_size}; exefs = {"main": "m"}
            data = {"status": "pass", "storage": "nup-cache-v1", "exefs_sha256": exefs,
                    "nup_header_sha256": "h", "nup_toc_sha256": "t", "nup_size": identity["size"],
                    "nup_path": str(nup), "cache_size": 4, "cache_sha256": cache.digest(folder / "cache.bin")}
            (folder / "index.json").write_text(json.dumps(data))
            first = cache.resource_cache(folder, identity, exefs, nup, None)
            self.assertIsNotNone(first)
            with patch.object(cache, "digest", wraps=cache.digest) as hashes:
                self.assertEqual(cache.resource_cache(folder, identity, exefs, nup, first), first)
                self.assertFalse(any(Path(c.args[0]).name == "cache.bin" for c in hashes.call_args_list))
            (folder / "cache.bin").write_bytes(b"BAD!")
            os.utime(folder / "cache.bin", ns=(first["mtime"] + 1000000, first["mtime"] + 1000000))
            self.assertIsNone(cache.resource_cache(folder, identity, exefs, nup, first))
            self.assertIsNone(cache.resource_cache(folder, {**identity, "toc": "wrong"}, exefs, nup, first))

    @unittest.skipUnless(os.name == "nt", "Windows file locking")
    def test_lock_is_exclusive_and_released_after_worker_dies(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            command = [sys.executable, "-c", "import sys;from pathlib import Path;from compiler.cache import workspace_lock;"
                       "\nwith workspace_lock(Path(sys.argv[1])):\n print('locked',flush=True);sys.stdin.read()", str(out)]
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(ValueError, "already"):
                    with cache.workspace_lock(out):
                        pass
            finally:
                process.terminate(); process.communicate(timeout=10)
            with cache.workspace_lock(out):
                pass

    @unittest.skipUnless(os.name == "nt", "Windows compiler workflow")
    def test_repeated_build_source_update_corruption_and_failed_build_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); bundle = root / "bundle"; out = root / "game"; nup = root / "source.nup"
            nup.write_bytes(b"owned source package fixture")
            names = ("source/tools/hwaoi.py", "source/tools/hostmem.py", "source/tools/check_content.py", "source/recomp/gen.py",
                     "source/runtime/a.c", "llvm/bin/clang.exe", "deps/include/a.h", "bin/ninja.exe", "bin/zlib1.dll",
                     "translator/totk_xlate.exe", "translator/totk_xlate.id")
            manifest = {"source_revision": "fixture", "files": {p: self.put(bundle, p) for p in names}}
            identity = {"header": "header", "toc": "toc", "size": nup.stat().st_size}
            hashes = {"main": "fixture-module-hash"}; calls = []; fail = [False]
            def verify(folder):
                if not (folder / "main").is_file():
                    raise FileNotFoundError("missing synthetic module")
                return {"exefs_sha256": hashes}
            def extract(archive, folder, update=None):
                self.put(folder, "main")
            def tool(b, o, *args):
                action = args[2]; calls.append(action)
                if action == "prepare-romfs":
                    folder = o / "work/game/romfs"; self.put(folder, "cache.bin", b"owned cached resource")
                    index = {"status": "pass", "storage": "nup-cache-v1", "exefs_sha256": hashes,
                             "nup_header_sha256": "header", "nup_toc_sha256": "toc", "nup_size": identity["size"],
                             "nup_path": str(nup), "cache_size": (folder / "cache.bin").stat().st_size,
                             "cache_sha256": cache.digest(folder / "cache.bin")}
                    (folder / "index.json").write_text(json.dumps(index))
                elif action == "generate":
                    folder = o / "work/generated/hwaoi196608-full"; self.put(folder, "main/a.c", b"owned generated fixture")
                    self.put(folder, "hwaoi-input.json", b"{}")
                    (folder / "hwaoi-generation.json").write_text(json.dumps({"status": "pass", "input_sha256": cache.digest(folder / "hwaoi-input.json")}))
                elif action == "build":
                    if fail[0]:
                        raise ValueError("synthetic compile failure")
                    self.put(o, "work/build/hwaoi196608/Hyrule Warriors AOI Recomp Optimized.exe", b"owned output fixture")
                else:
                    self.fail(action)
            disk = type("Disk", (), {"free": 60 << 30})()
            with patch.object(backend, "bundle_check", return_value=manifest), patch.object(backend, "inspect_nup", return_value=identity), \
                 patch.object(backend.hwaoi, "verify", side_effect=verify), \
                 patch.object(backend, "extract_game_exefs", side_effect=extract), patch.object(backend, "run_tool", side_effect=tool), \
                 patch.object(backend, "install_game_dlc", return_value={}), \
                 patch.object(backend.shutil, "disk_usage", return_value=disk), contextlib.redirect_stdout(io.StringIO()):
                backend.build(bundle, nup, out)
                self.assertEqual(calls, ["prepare-romfs", "generate", "build"])
                self.put(out, "work/saves/DATA.BIN", b"keep save")
                ini = out / "work/build/hwaoi196608/hwaoi.ini"
                ini.write_text("[display]\noutput_res=1920x1080\nvolume=42\n")
                original_mtime = (out / "runtime/a.c").stat().st_mtime_ns
                calls.clear(); backend.build(bundle, nup, out)
                self.assertEqual(calls, ["build"])
                self.assertEqual((out / "runtime/a.c").stat().st_mtime_ns, original_mtime)
                self.assertIn("volume=42", ini.read_text())
                manifest["files"]["source/runtime/a.c"] = self.put(bundle, "source/runtime/a.c", b"new runtime")
                calls.clear(); backend.build(bundle, nup, out)
                self.assertEqual(calls, ["build"])
                (out / "work/generated/hwaoi196608-full/main/a.c").write_bytes(b"damaged generated output")
                calls.clear(); fail[0] = True
                with self.assertRaisesRegex(ValueError, "compile failure"):
                    backend.build(bundle, nup, out)
                self.assertEqual(calls, ["generate", "build"])
                self.assertEqual(json.loads((out / backend.STATE).read_text())["status"], "incomplete")
                calls.clear(); fail[0] = False; backend.build(bundle, nup, out)
                self.assertEqual(calls, ["build"])
                self.assertEqual((out / "work/saves/DATA.BIN").read_bytes(), b"keep save")
                identity["toc"] = "another source"
                with self.assertRaisesRegex(ValueError, "different source"):
                    backend.build(bundle, nup, out)


if __name__ == "__main__":
    unittest.main()
