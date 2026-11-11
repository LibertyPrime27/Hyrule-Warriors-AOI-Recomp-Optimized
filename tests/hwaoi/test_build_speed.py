"""Memory-aware concurrency and real incremental compiler/dependency invalidation."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import build_game
sys.path.insert(0, str(ROOT / "recomp"))
import layout
import gen


class BuildSpeedTests(unittest.TestCase):
    def test_worker_budget_and_layout_mtimes(self):
        self.assertEqual(build_game.compile_jobs(0, 24, 20 << 30), 8)
        self.assertEqual(build_game.compile_jobs(0, 24, 5 << 30), 4)
        self.assertEqual(build_game.compile_jobs(8, 24, 2 << 30), 1)
        self.assertEqual(build_game.compile_jobs(2, 24, 20 << 30), 2)
        self.assertEqual(build_game.compile_jobs(0, 1, 20 << 30), 1)
        with self.assertRaises(ValueError):
            build_game.compile_jobs(-1, 24, 20 << 30)
        with self.assertRaises(ValueError):
            gen.pick_jobs(-1)
        with patch.object(gen.os, "cpu_count", return_value=24), patch.object(gen.hostmem, "available_ram", return_value=12 << 30), \
             patch.object(gen.hostmem, "available_commit", return_value=20 << 30):
            self.assertEqual(gen.pick_jobs(0)[0], 6)
            self.assertEqual(gen.pick_jobs(2)[0], 2)
        with patch.object(gen.hostmem, "available_ram", return_value=20 << 30), \
             patch.object(gen.hostmem, "available_commit", return_value=2 << 30):
            self.assertEqual(gen.pick_jobs(8)[0], 1)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp); mods = [("main", 0x100000, 4096)]
            layout.write(mods, out); old = (out / "layout.h").stat().st_mtime_ns
            layout.write(mods, out); self.assertEqual((out / "layout.h").stat().st_mtime_ns, old)
            layout.write([("main", 0x200000, 4096)], out)
            self.assertIn("0x200000", (out / "layout.h").read_text())

    @unittest.skipUnless(os.name == "nt" and Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe").is_file(), "Windows Clang/Ninja")
    def test_ninja_noop_parallelism_and_compiler_stamp(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-build speed-") as tmp:
            root = Path(tmp); out = root / "out"; out.mkdir()
            a = root / "a.c"; b = root / "b.c"; stamp = root / "compiler identity.json"
            a.write_text("int a(void){return 1;}"); b.write_text("int b(void){return 2;}"); stamp.write_text("one")
            (out / "modules.c").write_text("int a(void);int b(void);int main(void){return a()+b()!=3;}")
            values = {"CLANG": Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe"), "DEPS": Path("F:/TotKDependencies/runtime/deps")}
            with patch.multiple(build_game, **values), patch.object(build_game, "runtime_sources", side_effect=lambda *args: []):
                def generate(jobs):
                    text, objs, exe = build_game.ninja_file(root, out, {"main": [a, b]}, jobs, toolchain_stamp=stamp)
                    (root / "build.ninja").write_text(text)
                    self.assertEqual([p.name for p in objs["main"]], ["a.o", "b.o"])
                    return exe
                exe = generate(2)
                def run():
                    result = subprocess.run(["F:/Ninja/ninja.exe", "-f", str(root / "build.ninja"), "-j", "2"],
                                            cwd=ROOT, capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    return result.stdout
                self.assertEqual(run().count("CC "), 3)
                self.assertEqual(subprocess.run([str(exe)], timeout=5).returncode, 0)
                generate(1); self.assertIn("no work to do", run())
                stamp.write_text("two"); self.assertEqual(run().count("CC "), 3)
                self.assertIn("no work to do", run())
                # Large compile units get their own one-job pool; link order is unchanged.
                with a.open("ab") as stream:
                    stream.truncate(5 << 20)
                text, objs, _ = build_game.ninja_file(root, out, {"main": [b, a]}, 4)
                self.assertIn("pool large_gen\n  depth = 1", text)
                self.assertLess(text.index(": cc_gen " + build_game.ninja_path(a)), text.index(": cc_gen " + build_game.ninja_path(b)))
                self.assertEqual([p.name for p in objs["main"]], ["b.o", "a.o"])


if __name__ == "__main__":
    unittest.main()
