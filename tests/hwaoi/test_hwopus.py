"""Compile actual hwopus service against existing host libopus; synthetic PCM only."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CLANG = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
DEPS = Path("F:/TotKDependencies/runtime/deps")


@unittest.skipUnless(os.name == "nt" and CLANG.is_file() and (DEPS / "lib/libopus.dll.a").is_file(),
                     "existing Windows Clang/libopus required")
class HardwareOpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="hwaoi-hwopus-")
        cls.exe = Path(cls.temp.name) / "hwopus-test.exe"
        shutil.copy2(DEPS / "bin/libopus-0.dll", cls.exe.parent / "libopus-0.dll")
        cmd = [str(CLANG), "-std=gnu11", "-O1", "-Wall", "-Wextra", "-march=x86-64-v3",
               "-I", str(ROOT / "runtime"), "-I", str(DEPS / "include"),
               str(ROOT / "tests/hwaoi/hwopus_fixture.c"), "-L", str(DEPS / "lib"), "-lopus", "-o", str(cls.exe)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode:
            cls.temp.cleanup()
            raise AssertionError(result.stdout + result.stderr)
        cls.env = dict(os.environ)
        path_key = next((key for key in cls.env if key.lower() == "path"), "PATH")
        cls.env[path_key] = str(DEPS / "bin") + os.pathsep + cls.env.get(path_key, "")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, name):
        return subprocess.run([str(self.exe), name], env=self.env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=15)

    def test_real_six_channel_multistream_decode_reset_mapping_and_lifetime(self):
        r = self.run_case("positive")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no live sessions; real libopus multistream PCM", r.stdout)

    def test_malformed_unsupported_and_unknown_requests_fail_visibly(self):
        # Each fatal ends its process, like production; never resume a held lock.
        for name in ("option", "reserved", "rate", "channels", "streams", "coupled", "mapping",
                     "short-tmem", "bad-handle", "reset", "shape", "unknown-manager", "unknown-decoder"):
            with self.subTest(name=name):
                r = self.run_case(name)
                self.assertEqual(r.returncode, 42, r.stdout + r.stderr)
                self.assertTrue("hwopus:" in r.stderr or "unknown command" in r.stderr, r.stderr)


if __name__ == "__main__":
    unittest.main()
