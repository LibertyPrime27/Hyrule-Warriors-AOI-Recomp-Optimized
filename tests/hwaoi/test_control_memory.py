"""Exercise actual NVN control exhaustion, callback routing and recording retirement."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path("F:/llvm-mingw/bin/clang.exe")
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Windows Clang required")
class ControlMemoryTests(unittest.TestCase):
    def test_exhaustion_callbacks_and_handle_lifetimes(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-control-memory-") as temporary:
            directory = Path(temporary)
            source = (ROOT / "runtime/nvn_api_cmd.c").read_text(encoding="utf-8")
            declaration = "NVN_API(nvnCommandBufferEndRecording)"
            # Self-contained negative control: omit exhaustion accounting while
            # retaining the actual recorder/counters/providers. No moving HEAD
            # reference or Git installation is needed to detect a regression.
            current = function(source, declaration)
            start = current.index("    // Checking only ctl_size")
            end = current.index("    put_hdr(", start)
            omitted = current[:start] + ("    if (!o->ctl_size) memory_event(c, o, "
                                        "NVN_COMMAND_BUFFER_MEMORY_EVENT_OUT_OF_CONTROL_MEMORY, MIN_CONTROL_SIZE);\n") + current[end:]
            baseline = directory / "old-exhaustion.c"
            baseline.write_text(source.replace(current, omitted), encoding="utf-8")
            for optimization in ("0", "2"):
                for name, production in (("working", ROOT / "runtime/nvn_api_cmd.c"), ("baseline", baseline)):
                    executable = directory / f"{name}-o{optimization}.exe"
                    compiled = subprocess.run([str(CC), "-std=gnu11", f"-O{optimization}", "-Wall", "-Wextra", "-Werror",
                        "-Wno-missing-field-initializers", "-ffunction-sections", "-fdata-sections", "-I", str(ROOT / "runtime"),
                        str(ROOT / "tests/hwaoi/control_memory_fixture.c"), str(production), "-Wl,--gc-sections", "-o", str(executable)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                    self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
                    if name == "baseline":
                        run = subprocess.run([str(executable), "recycling"], capture_output=True, text=True, timeout=20)
                        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                        self.assertIn("callback_count==i/4", run.stderr)
                        continue
                    for case in ("packed", "recycling", "initial", "boundary", "tail", "pending", "short", "missing", "absent",
                                 "disabled", "noop", "short-callback", "wrong-memory", "overflow-add", "overflow-end", "null-add"):
                        with self.subTest(optimization=optimization, case=case):
                            run = subprocess.run([str(executable), case], capture_output=True, text=True, timeout=20)
                            rejected = case not in ("packed", "recycling", "initial", "boundary", "tail", "pending")
                            self.assertEqual(run.returncode, 73 if rejected else 0, run.stdout + run.stderr)
                            if rejected:
                                self.assertIn("nonnull, non-wrapping" if case in ("overflow-add", "null-add") else "no space", run.stderr)


if __name__ == "__main__":
    unittest.main()
