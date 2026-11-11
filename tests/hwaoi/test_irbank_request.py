"""Production shader-request/cache serialization of the private IR-bank marker.

Reuses the existing CPU-only shader fixture. No translator/GPU/game is run.
"""
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Windows Clang required")
class IrBankRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="irbank-request-")
        cls.directory = Path(cls.tmp.name)
        source = (ROOT / "tests/geometry_shader_cache.c").read_text(encoding="utf-8")
        source = source.replace("@SHADER_SOURCE@", (ROOT / "runtime/gfx_shader.c").as_posix())
        source = source.replace("@HASH_CALL@", "build_code_hash(stage,control,code,size,c1,c1_size,input,hash)")
        fixture = cls.directory / "fixture.c"
        fixture.write_text("#define FIXTURE_GEOMETRY 1\n" + source, encoding="utf-8")
        cls.exe = cls.directory / "fixture.exe"
        runtime = ROOT / "runtime"
        command = [str(CC), "-std=gnu11", "-O1", "-ffunction-sections", "-fdata-sections",
                   "-I", str(runtime), str(fixture), str(runtime / "sha256.c"),
                   str(runtime / "platform/win32.c"), str(runtime / "platform/common.c"),
                   "-Wl,--gc-sections", "-lbcrypt", "-lpdh", "-lpsapi", "-lrstrtmgr",
                   "-lshell32", "-lwinmm", "-o", str(cls.exe)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if result.returncode:
            cls.tmp.cleanup()
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_fixture(self, mode, *args):
        result = subprocess.run([str(self.exe), mode, str(self.directory / self._testMethodName), *map(str, args)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def packet(self, marker):
        # Minimal synthetic CS scheduling group. req_init consumes only the
        # framed bytes; this test makes no shader semantic claim.
        code = struct.pack("<4Q", 0, *([0xe30000000007000f] * 3))
        control = bytearray(0x880)
        struct.pack_into("<II", control, 0x700, len(code), len(code))
        control[0x850:0x854] = marker
        code_path = self.directory / "code.bin"
        control_path = self.directory / "control.bin"
        code_path.write_bytes(code)
        control_path.write_bytes(control)
        return code_path, control_path

    def test_exact_marker_gates_job_and_changes_identity(self):
        normal = self.run_fixture("store-input", 5, 0, *self.packet(bytes(4)))
        marked = self.run_fixture("store-input", 5, 0, *self.packet(b"HIR1"))
        self.assertTrue(normal["accepted"] and marked["accepted"])
        self.assertNotIn("hwaoi_ir_banks", normal["request"]["job"])
        self.assertEqual(marked["request"]["job"]["hwaoi_ir_banks"], 1)
        for field in ("hash", "iid", "key"):
            self.assertNotEqual(normal[field], marked[field])
        for marker in (b"HIR0", b"HIR2", b"XIR1"):
            other = self.run_fixture("request", 5, 0, *self.packet(marker))
            self.assertNotIn("hwaoi_ir_banks", other["request"]["job"])

    def test_marker_survives_stored_input_reconstruction(self):
        original = self.run_fixture("store-input", 5, 0, *self.packet(b"HIR1"))
        restored = self.run_fixture("load-input", original["iid"])
        self.assertTrue(restored["accepted"] and restored["recreated"])
        self.assertEqual(original["iid"], restored["iid"])
        self.assertEqual(restored["request"]["job"]["hwaoi_ir_banks"], 1)


if __name__ == "__main__":
    unittest.main()
