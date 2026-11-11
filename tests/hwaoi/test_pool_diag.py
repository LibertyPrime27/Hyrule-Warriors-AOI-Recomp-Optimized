"""Optional pool observations preserve original function state and reject unknown profiles."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Windows Clang required")
class PoolDiagTests(unittest.TestCase):
    def test_state_preservation_and_hash_guard(self):
        with tempfile.TemporaryDirectory(prefix="pool-diag-") as temporary:
            for optimization in ("0", "2"):
                exe = Path(temporary) / f"diag-{optimization}.exe"
                result = subprocess.run([
                    str(CC), "-std=gnu11", f"-O{optimization}", "-Wall", "-Wextra", "-Werror",
                    "-march=x86-64-v3", "-I", str(ROOT / "runtime"),
                    str(ROOT / "tests/hwaoi/pool_diag_fixture.c"), "-o", str(exe),
                ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for case in ("disabled", "observe", "nested", "fibers", "capacity", "hash", "module"):
                    with self.subTest(optimization=optimization, case=case):
                        run = subprocess.run([str(exe), case], capture_output=True, text=True,
                                             encoding="utf-8", errors="replace", timeout=10)
                        self.assertEqual(run.returncode, 70 if case in ("hash", "module") else 0,
                                         run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
