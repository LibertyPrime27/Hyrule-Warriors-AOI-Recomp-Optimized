"""Portable compiler integrity and output refusal paths; no game content needed."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import struct
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from compiler import backend
sys.path.insert(0, str(ROOT / "tests/prototype"))
from test_formats import make_nup, refresh_toc_hash, TITLE_ID
from hwaoi_romfs import romfs_inventory, select_romfs


class CompilerFrontendTests(unittest.TestCase):
    def standalone(self, root):
        payloads = {name: ("synthetic executable " + name).encode() for name in ("main", "main.npdm", "rtld", "sdk", "subsdk0")}
        raw, _ = make_nup([{"kind": 1, "flags": 1, "title_id": TITLE_ID | 0x800, "version": 10,
                            "files": [(n, data, 0) for n, data in payloads.items()]}])
        struct.pack_into("<I", raw, 0x80 + 0x30 + 0x10, 1)
        refresh_toc_hash(raw)
        path = root / "complete-update.nup";path.write_bytes(raw)
        expected = {"title_id": f"{TITLE_ID:016X}", "ncm_version": 10,
                    "exefs_sha256": {n: hashlib.sha256(data).hexdigest() for n, data in payloads.items()}}
        with backend.Nup(path) as archive:
            expected["romfs_inventory"] = romfs_inventory(select_romfs(archive, archive.packages))
        return path, expected, payloads

    def test_log_tee_survives_dump_names_a_legacy_console_cannot_encode(self):
        import io
        name = "Hyrule Warriors꞉ Age of Imprisonment + Update 1.0.3 + DLC.nup"
        console, log = io.TextIOWrapper(io.BytesIO(), encoding="cp1252"), io.StringIO()
        tee = backend.Tee(console, log)
        tee.write(f"+ prepare-romfs --nup {name}\n")
        self.assertEqual(log.getvalue(), f"+ prepare-romfs --nup {name}\n")
        console.seek(0)
        self.assertIn("Warriors? Age", console.read())

    def test_standalone_preflight_and_extraction_use_the_same_exact_package(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp);path, expected, payloads = self.standalone(folder)
            original = path.read_bytes()
            with patch.object(backend, "nup_profile", return_value=expected):
                identity = backend.inspect_nup(path)
                result = backend.extract_game_exefs(path, folder / "exefs")
            self.assertEqual(identity["size"], len(original))
            self.assertEqual(result["selected_packages"], [0])
            self.assertEqual({p.name: p.read_bytes() for p in (folder / "exefs").iterdir()}, payloads)
            self.assertEqual(path.read_bytes(), original)

    def test_wrong_module_and_resource_hashes_refuse_before_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp);path, expected, _ = self.standalone(folder)
            for what, bad in (("modules", {**expected, "exefs_sha256": {**expected["exefs_sha256"], "main": "0" * 64}}),
                              ("resources", {**expected, "romfs_inventory": {**expected["romfs_inventory"], "sha256": "0" * 64}})):
                output = folder / what
                with patch.object(backend, "nup_profile", return_value=bad):
                    with self.assertRaises(ValueError):backend.inspect_nup(path)
                    with self.assertRaises(ValueError):backend.extract_game_exefs(path, output)
                self.assertFalse(output.exists())

    def test_payload_hash_is_still_checked_after_table_identity_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp);path, expected, _ = self.standalone(folder)
            with backend.Nup(path) as archive: offset = archive.files[0].offset
            raw = bytearray(path.read_bytes());raw[offset] ^= 1;path.write_bytes(raw)
            with patch.object(backend, "nup_profile", return_value=expected):
                backend.inspect_nup(path)  # inventory identity is explicitly a TOC check
                with self.assertRaisesRegex(ValueError, "member SHA-256 mismatch"):
                    backend.extract_game_exefs(path, folder / "bad-exefs")
            self.assertEqual(path.read_bytes(), raw)

    def bundle(self, root):
        names = ("python/python.exe", "llvm/bin/clang.exe", "bin/ninja.exe", "bin/zlib1.dll",
                 "translator/totk_xlate.exe", "translator/totk_xlate.id", "deps/include/vma/vk_mem_alloc.h",
                 "source/tools/hwaoi.py", "source/tools/hwaoi_romfs.py")
        files = {}
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"owned synthetic tool fixture\n")
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {"format": "hwaoi-compiler-v1", "source_revision": "synthetic", "files": files}
        (root / "bundle.json").write_text(json.dumps(manifest))
        return manifest

    def test_valid_bundle_tamper_missing_tool_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "compiler"
            root.mkdir()
            manifest = self.bundle(root)
            self.assertEqual(backend.bundle_check(root)["source_revision"], "synthetic")
            (root / "bin/ninja.exe").write_bytes(b"different")
            with self.assertRaisesRegex(ValueError, "changed"):
                backend.bundle_check(root)
            backend.bundle_check(root, defer_materialized=True)  # unused compiler input is verified by synchronization
            for name in ("source/tools/hwaoi.py", "source/tools/hwaoi_romfs.py"):
                self.bundle(root)
                (root / name).write_bytes(b"changed validator")
                with self.assertRaisesRegex(ValueError, "changed"):
                    backend.bundle_check(root, defer_materialized=True)
            manifest = self.bundle(root)
            del manifest["files"]["bin/ninja.exe"]
            (root / "bundle.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                backend.bundle_check(root)
            manifest["files"]["../outside"] = "0" * 64
            (root / "bundle.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Missing"):
                backend.bundle_check(root)

    def test_output_never_overwrites_user_files_bundle_or_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "compiler"
            bundle.mkdir()
            nup = root / "my-source.nup"
            nup.write_bytes(b"synthetic invalid input")
            occupied = root / "existing"
            occupied.mkdir()
            keep = occupied / "keep.txt"
            keep.write_text("keep me")
            for output in (occupied, bundle / "build", root):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    backend.fresh_output(output, bundle, nup)
            self.assertEqual(keep.read_text(), "keep me")
            fresh = root / "fresh"
            with patch.object(backend.shutil, "disk_usage", return_value=type("Disk", (), {"free": 40 << 30})()):
                self.assertEqual(backend.fresh_output(fresh, bundle, nup), fresh.resolve())
            self.assertFalse(fresh.exists())
            with patch.object(backend.shutil, "disk_usage", return_value=type("Disk", (), {"free": 1 << 30})()):
                with self.assertRaisesRegex(ValueError, "20 GiB"):
                    backend.fresh_output(fresh, bundle, nup)

    def test_malformed_nup_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.nup"
            data = b"not a supported game package" * 8
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                backend.inspect_nup(path)
            self.assertEqual(path.read_bytes(), data)

    def test_host_environment_overrides_do_not_leak_into_user_build(self):
        with patch.dict(os.environ, {"HWAOI_FPS_LIMIT": "120", "TOTK_EXEFS": "wrong",
                                     "DKB_XLATE": "wrong", "PYTHONPATH": "wrong"}):
            env = backend.clean_environment(Path("X:/portable"))
            for key in ("HWAOI_FPS_LIMIT", "TOTK_EXEFS", "DKB_XLATE", "PYTHONPATH"):
                self.assertNotIn(key, env)
            self.assertNotIn("F:/Python", env["PATH"])


if __name__ == "__main__":
    unittest.main()
