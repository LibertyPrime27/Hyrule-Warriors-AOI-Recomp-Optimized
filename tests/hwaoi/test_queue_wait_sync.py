"""Run the actual NVN queue/command wait code against a latched native backend."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
CLANG = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")


@unittest.skipUnless(CLANG.is_file(), "project Clang is not installed")
class QueueWaitSyncTests(unittest.TestCase):
    def test_native_fence_ordering_and_failure_paths(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tests/nvn_wait_sync_test.py"), "--cc", str(CLANG)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("0 failures", completed.stdout)
        self.assertIn("six malformed-object/state negative cases passed", completed.stdout)
        self.assertIn("seven invalid/cyclic/unsupported cases rejected", completed.stdout)


if __name__ == "__main__":
    unittest.main()
