"""Exercise bounded Windows process ownership with an original tiny fixture."""
import os
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from hwaoi_probe import capture


@unittest.skipUnless(os.name == "nt" and Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe").is_file(), "Windows Clang required")
class ProbeRunnerTests(unittest.TestCase):
    def test_exit_and_timeout_are_distinct(self):
        for folder in (ROOT / "work/build", ROOT / "work/reports"):
            folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="runner-", dir=ROOT / "work/build") as build:
            with tempfile.TemporaryDirectory(prefix="runner-", dir=ROOT / "work/reports") as reports:
                source = Path(build) / "fixture.c"
                source.write_text(r'''
#include <windows.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
static LRESULT CALLBACK window_proc(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (m == WM_CLOSE) { puts("owned window close observed"); PostQuitMessage(0); return 0; }
    return DefWindowProcW(h, m, w, l);
}
int main(int n, char **v) {
    if (n > 1 && !strcmp(v[1], "--boot")) Sleep(10000);
    if (n > 1 && !strcmp(v[1], "--play-experimental")) {
        const char *names[] = {"HWAOI_SHOTS", "HWAOI_PROFILE_IP_ONLY", "HWAOI_PROFILE_MAIN", "HWAOI_PROFILE_FRAMES", "HWAOI_INPUT_FEED"};
        for (int i = 0; i < 5; i++) printf("fixture-env %s=%s\n", names[i], getenv(names[i]) ? getenv(names[i]) : "absent");
        WNDCLASSW wc = {0}; wc.lpfnWndProc = window_proc; wc.lpszClassName = L"ProbeRunnerFixture";
        wc.hInstance = GetModuleHandleW(NULL);
        if (!RegisterClassW(&wc) || !CreateWindowW(wc.lpszClassName, L"hidden diagnostic fixture",
                0, 0, 0, 1, 1, NULL, NULL, wc.hInstance, NULL)) return 3;
        fprintf(stderr, "HWAoI progress t=1.250 present=240 texture=32x32 crop=0,0,16,16 shader=0 lowering=0 validation=0 unimplemented=0 skipped=0\n");
        fflush(stderr);
        MSG msg; while (GetMessageW(&msg, NULL, 0, 0) > 0) { TranslateMessage(&msg); DispatchMessageW(&msg); }
    }
    puts("synthetic fixture"); return 0;
}
''', encoding="utf-8")
                executable = Path(build) / "fixture.exe"
                subprocess.run([os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe", str(source), "-o", str(executable)], check=True)
                normal = capture(executable, "--selftest", Path(reports) / "normal", 5)
                self.assertEqual(normal["exit_code"], 0)
                self.assertFalse(normal["timed_out"])
                timeout = capture(executable, "--boot", Path(reports) / "timeout", 1)
                self.assertTrue(timeout["timed_out"])
                self.assertNotEqual(timeout["exit_code"], 0)
                script = Path(build) / "input.txt"
                script.write_bytes(b"360 A 20\n")
                graceful = capture(executable, "--play-experimental", Path(reports) / "graceful", 1,
                                   input_script=script)
                self.assertTrue(graceful["timed_out"])
                self.assertEqual(graceful["exit_code"], 0)
                self.assertGreater(graceful["supervisor_close_requests"], 0)
                self.assertEqual(graceful["last_progress"]["present"], 240)
                self.assertEqual(graceful["last_progress"]["guest_elapsed_seconds"], 1.25)
                self.assertEqual(graceful["input_script_sha256"], hashlib.sha256(script.read_bytes()).hexdigest())
                retained = Path(graceful["runtime_options"]["HWAOI_INPUT_SCRIPT"])
                self.assertEqual(retained.parent, Path(reports) / "graceful")
                self.assertEqual(retained.read_bytes(), script.read_bytes())
                self.assertIn("owned window close observed", Path(graceful["stdout"]).read_text())
                feed = Path(reports) / "feed.txt"
                feed.write_text("1 0 0 0 0 0 0\n", encoding="ascii")
                profiled = capture(executable, "--play-experimental", Path(reports) / "profiled", 1,
                                   shot_every=0, profile_main_ip=True, profile_frames="1400-2000", input_feed=feed)
                output = Path(profiled["stdout"]).read_text()
                for name, value in (("SHOTS", "0"), ("PROFILE_IP_ONLY", "1"),
                                    ("PROFILE_MAIN", "1"), ("PROFILE_FRAMES", "1400-2000")):
                    self.assertIn(f"fixture-env HWAOI_{name}={value}", output)
                    self.assertEqual(profiled["runtime_options"]["HWAOI_" + name], value)
                self.assertEqual(profiled["runtime_options"]["HWAOI_INPUT_FEED"], str(feed.resolve()))
                self.assertIn("fixture-env HWAOI_INPUT_FEED=" + str(feed.resolve()), output)
                with self.assertRaisesRegex(ValueError, "file under work/reports"):
                    capture(executable, "--play-experimental", Path(reports) / "invalid-feed", 1, input_feed=script)
                for value in ("2000-1400", "1400-1400", "-1-2000", "0-18446744073709551616", "all"):
                    with self.assertRaisesRegex(ValueError, "valid LO-HI"):
                        capture(executable, "--play-experimental", Path(reports) / "invalid-range", 1,
                                profile_main_ip=True, profile_frames=value)
                with self.assertRaisesRegex(ValueError, "automatic screenshots"):
                    capture(executable, "--play-experimental", Path(reports) / "invalid-stall", 1,
                            shot_every=0, threads_on_stall=True)
                with self.assertRaisesRegex(ValueError, "1..180"):
                    capture(executable, "--play-experimental", Path(reports) / "too-long", 181)
                with self.assertRaisesRegex(ValueError, "1..1800"):
                    capture(executable, "--play-experimental", Path(reports) / "extended-too-long", 1801, extended_play=True)
                with self.assertRaisesRegex(ValueError, "extended deadline requires"):
                    capture(executable, "--probe-render", Path(reports) / "wrong-extended-mode", 1, extended_play=True)
                with self.assertRaisesRegex(ValueError, "requires play-experimental"):
                    capture(executable, "--probe-render", Path(reports) / "wrong-mode", 1, input_script=script)


if __name__ == "__main__":
    unittest.main()
