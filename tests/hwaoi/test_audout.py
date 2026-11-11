"""Actual AudioOut adapter with deterministic synthetic clocks/SDL/kernel mocks.

No audio device, guest executable, ROM or firmware is used by this fixture.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CLANG = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
SDL_INCLUDE = Path("F:/TotKDependencies/runtime/deps/include")


@unittest.skipUnless(os.name == "nt" and CLANG.is_file()
                     and (SDL_INCLUDE / "SDL3/SDL.h").is_file(),
                     "Windows Clang and existing SDL3 headers required")
class AudioOutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="audout-fixture-")
        cls.executable = Path(cls.temporary.name) / "audout-fixture.exe"
        command = [str(CLANG), "-std=gnu11", "-O1", "-flto", "-march=x86-64-v3",
                   "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
                   "-Wl,--exclude-all-symbols", "-DSDL_STATIC_LIB", "-DSDL_MAIN_HANDLED",
                   "-I", str(ROOT / "runtime"), "-I", str(SDL_INCLUDE),
                   str(ROOT / "tests/hwaoi/audout_fixture.c"), "-o", str(cls.executable)]
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
        if result.returncode:
            cls.temporary.cleanup()
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_case(self, name):
        result = subprocess.run([str(self.executable), name], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check passed", result.stdout)

    def test_manager_protocol_and_legacy_auto_select_buffers(self):
        self.run_case("manager")

    def test_null_playback_accumulates_fractional_frames(self):
        self.run_case("fractional")

    def test_stopped_buffers_do_not_complete_and_resume_keeps_progress(self):
        self.run_case("pause")

    def test_buffer_offsets_snapshot_and_input_byte_consumption(self):
        self.run_case("snapshot")

    def test_invalid_buffer_ranges_alignment_and_duplicates_rejected(self):
        self.run_case("invalid")

    def test_queue_capacity_includes_uncollected_releases(self):
        self.run_case("capacity")

    def test_fifo_releases_event_and_partial_c_buffer_draining(self):
        self.run_case("fifo")

    def test_event_copy_handle_ownership_survives_session_close(self):
        self.run_case("event-life")

    def test_flush_volume_and_command_failure_paths(self):
        self.run_case("controls")


if __name__ == "__main__":
    unittest.main()
