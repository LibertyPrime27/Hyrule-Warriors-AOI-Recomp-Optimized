"""Synthetic packets exercise native zlib and strict adapter fallback/verify behavior."""
import os
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
DLL = Path("F:/GCC/ucrt64/bin/zlib1.dll")


def packet(payload):
    n = len(payload)
    key = 0x1020304050607080 + n % 32
    if n >= 16:
        sample = min((int.from_bytes(payload[8:12], "little") ^ key) % 32, n) & 24
    else:
        sample = n // 8 * 8
    offset = ((int.from_bytes(payload[:8], "little") ^ key) % n if n >= 8 else key ^ n) // 8 * 8
    if offset + sample > n:
        offset = 0
    checksum = key + sum(int.from_bytes(payload[i:i + 8], "little") for i in range(offset, offset + sample, 8))
    return struct.pack("<Q", checksum % (1 << 64)) + payload


@unittest.skipUnless(os.name == "nt" and CC.is_file() and DLL.is_file(), "Existing Windows Clang/zlib required")
class NativeInflateTests(unittest.TestCase):
    def test_native_packets_and_failure_paths(self):
        with tempfile.TemporaryDirectory(prefix="native-inflate-") as temporary:
            tmp = Path(temporary)
            rng = random.Random(1701)
            samples = [bytes(16384), bytes(range(256)) * 64, rng.randbytes(16384)]
            for optimization in ("0", "2"):
                exe = tmp / f"inflate-{optimization}.exe"
                compile_run = subprocess.run([
                    str(CC), "-std=gnu11", f"-O{optimization}", "-Wall", "-Wextra", "-Werror",
                    "-march=x86-64-v3", "-I", str(ROOT / "runtime"),
                    str(ROOT / "tests/hwaoi/native_inflate_fixture.c"), "-o", str(exe),
                ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                self.assertEqual(compile_run.returncode, 0, compile_run.stdout + compile_run.stderr)
                for index, expected in enumerate(samples):
                    (tmp / "expected.bin").write_bytes(expected)
                    streams = [zlib.compress(expected), zlib.compress(expected, 0), zlib.compress(expected) + b"trailing"]
                    for stream in streams:
                        (tmp / "packet.bin").write_bytes(packet(stream))
                        modes = ("fast", "verify", "compose", "split") if index else (
                            "fast", "verify", "compose", "split", "off", "hash", "module", "extent", "overflow", "path", "library", "mismatch",
                            "checksum", "readonly", "overlap", "length", "input", "adler",
                            "nonheap", "jobregion", "uncommitted", "query", "keys",
                        )
                        for mode in modes:
                            if mode == "adler" and stream.endswith(b"trailing"):
                                continue  # Trailing bytes are deliberately accepted by the original.
                            with self.subTest(optimization=optimization, sample=index, size=len(stream), mode=mode):
                                run = subprocess.run([str(exe), mode, str(tmp / "packet.bin"),
                                                      str(tmp / "expected.bin"), str(DLL)],
                                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
                                want = 70 if mode in ("hash", "module", "extent", "overflow", "path", "library", "mismatch") else 0
                                self.assertEqual(run.returncode, want, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
