"""Run the optional allocator adapter against controlled allocation progress/failures."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Existing Windows Clang required")
class PoolBackpressureTests(unittest.TestCase):
    def test_context_retry_gates_and_bounded_failure(self):
        with tempfile.TemporaryDirectory(prefix="pool-backpressure-") as temporary:
            for optimization in ("0", "2"):
                exe = Path(temporary) / f"pool-{optimization}.exe"
                build = subprocess.run([
                    str(CC), "-std=gnu11", f"-O{optimization}", "-Wall", "-Wextra", "-Werror",
                    "-march=x86-64-v3", "-I", str(ROOT / "runtime"),
                    str(ROOT / "tests/hwaoi/pool_backpressure_fixture.c"), "-o", str(exe),
                ], capture_output=True, text=True, timeout=60)
                self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
                for mode in ("off", "unset", "recover", "success", "lr", "size", "near-submit", "wrong-submit-size", "badmode", "hash", "nohash",
                             "module", "extent", "caller-extent", "caller-partial-instruction", "caller-full-instruction", "overflow", "function", "timeout", "closing", "empty", "count",
                             "slot", "backing", "nonheap", "unaligned", "readonly", "parent", "parent-size", "parent-buffer",
                             "child-first", "child-second", "child-size", "child-near", "child-other", "child-timeout", "child-closing",
                             *(f"submit-{i}" for i in range(11)),
                             "wrapper-recover", "wrapper-success", "wrapper-timeout", "wrapper-closing",
                             "wrapper-lr", "wrapper-size", "wrapper-sp-zero", "wrapper-sp-align", "wrapper-sp-overflow",
                             "wrapper-sp-limit", "wrapper-fp", "wrapper-args", "wrapper-tag",
                             *(f"wrapper-return-{i}" for i in range(4)),
                             *(f"wrapper-chain-{i}" for i in range(3)),
                             *(f"wrapper-unreadable-{i}" for i in range(4))):
                    with self.subTest(optimization=optimization, mode=mode):
                        run = subprocess.run([str(exe), mode], capture_output=True, text=True, timeout=10)
                        expected = 0 if mode in ("off", "unset", "recover", "success", "lr", "size", "near-submit", "wrong-submit-size", "caller-full-instruction",
                                                "parent", "parent-size", "parent-buffer", "child-first", "child-second",
                                                "child-size", "child-near", "child-other") or mode.startswith("submit-") or (
                                                    mode.startswith("wrapper-") and mode not in ("wrapper-timeout", "wrapper-closing")) else 70
                        self.assertEqual(run.returncode, expected, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
