"""Per-stage binding of actual production helper with synthetic shader metadata.

No shader translator, GPU, guest module or firmware is used.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Windows Clang required")
class ProgramBindTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="program-bind-")
        cls.executables = []
        for optimization in ("0", "2"):
            executable = Path(cls.tmp.name) / f"program-bind-o{optimization}.exe"
            command = [str(CC), "-std=gnu11", f"-O{optimization}", "-Wall", "-Wextra", "-Werror",
                       "-march=x86-64-v3", "-I", str(ROOT / "runtime"),
                       str(ROOT / "tests/hwaoi/program_bind_fixture.c"), "-o", str(executable)]
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=60)
            if result.returncode:
                cls.tmp.cleanup()
                raise AssertionError(result.stdout + result.stderr)
            cls.executables.append(executable)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_case(self, name):
        for executable in self.executables:
            with self.subTest(optimization=executable.stem):
                result = subprocess.run([str(executable), name], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("check passed", result.stdout)

    def test_real_stage_mask_sequence_retains_vertex_and_fragment(self):
        self.run_case("sequence")

    def test_compute_and_graphics_bindings_are_independent(self):
        self.run_case("compute")

    def test_selected_mask_only_zero_mask_and_bad_mask_are_atomic(self):
        self.run_case("masks")

    def test_repeated_bind_and_refresh_do_not_allocate_new_serial(self):
        self.run_case("unchanged")

    def test_same_pointer_new_source_serial_invalidates_merged_program(self):
        self.run_case("serial")

    def test_all_graphics_mask_and_null_all_clear(self):
        self.run_case("all-clear")

    def test_shader_records_are_copied_without_transferring_control_ownership(self):
        self.run_case("ownership")

    def test_missing_and_malformed_lookup_results(self):
        self.run_case("lookup-failure")


if __name__ == "__main__":
    unittest.main()
