"""Synthetic-only tests: no game code or assets are committed here."""
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
import tempfile
import unittest

from hwrecomp.a64 import UnsupportedInstruction, instruction_support, translate


ROOT = Path(__file__).resolve().parents[2]
CLANG = os.environ.get("HWR_TEST_CLANG") or shutil.which("clang")
if not CLANG and Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe").is_file():
    CLANG = os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe"


def words(*values):
    return struct.pack("<" + "I" * len(values), *values)


def wide(value, rd=0, bits=64, op=2, half=0):
    return (0x12800000 | ((bits == 64) << 31) | (op << 29)
            | (half << 21) | (value << 5) | rd)


def arithmetic(rd, rn, value, bits=64, subtract=False, flags=False):
    return (0x11000000 | ((bits == 64) << 31) | (subtract << 30)
            | (flags << 29) | (value << 10) | (rn << 5) | rd)


def register_arithmetic(rd, rn, rm, bits=64, subtract=False, flags=False,
                        shift=0, count=0):
    return (0x0b000000 | ((bits == 64) << 31) | (subtract << 30)
            | (flags << 29) | (shift << 22) | (rm << 16)
            | (count << 10) | (rn << 5) | rd)


class DecoderTests(unittest.TestCase):
    def test_known_encodings(self):
        expected = {
            0xd503201f: "nop", 0xd65f03c0: "ret", 0xd61f0200: "br",
            0xd63f0200: "blr", 0x910003e0: "add_sub_immediate",
            0x2a0103e0: "logical_shifted", 0xa9bf7bfd: "pair",
            0xa8c17bfd: "pair", 0xf9400000: "memory",
            0xf8008420: "memory", 0x90000000: "adrp",
            0x10000000: "adr", 0x14000000: "b", 0x94000000: "bl",
            0x54000000: "b_cond", 0x34000000: "cb", 0x36000000: "tb",
        }
        for word, family in expected.items():
            self.assertEqual(instruction_support(word), family, hex(word))

    def test_reserved_unsupported_and_overlap_rejected(self):
        for word in (0, 0xd4000001, 0x4e20d400, 0x53000000,
                     0x52c00000, 0x0bc00000, 0x0b008000,
                     0xf8408400, 0xa8c10420, 0xa9400000,
                     0x3dc00000, 0xb9800000):
            self.assertIsNone(instruction_support(word), hex(word))
        with self.assertRaises(UnsupportedInstruction) as raised:
            translate(words(0xd503201f, 0xd4000001), 0x1000)
        self.assertEqual(raised.exception.address, 0x1004)
        self.assertEqual(raised.exception.word, 0xd4000001)

    def test_input_validation(self):
        for code, address, name in ((b"", 0, "f"), (b"1", 0, "f"),
                                    (words(0xd503201f), 1, "f"),
                                    (words(0xd503201f), 0, "f;evil")):
            with self.assertRaises(ValueError):
                translate(code, address, name)


@unittest.skipUnless(CLANG, "native C compiler unavailable; set HWR_TEST_CLANG")
class NativeTranslationTests(unittest.TestCase):
    def compile_and_run(self, functions, checks):
        declarations = "\n".join(f"int {name}(HwrContext *);" for name in functions)
        harness = ('#include "hwr_runtime.h"\n#include <stdio.h>\n#include <string.h>\n'
                   + declarations + '\n#define CHECK(x) do { if (!(x)) { '
                   'fprintf(stderr, "failed line %d: %s\\n", __LINE__, #x); return 1; } } while (0)\n'
                   + 'int main(void) { HwrContext c; uint8_t memory[128] = {0};\n'
                   + '(void)memory;\n' + checks + '\nreturn 0; }\n')
        with tempfile.TemporaryDirectory(prefix="hwr-native-") as temporary:
            directory = Path(temporary)
            paths = []
            for name, code in functions.items():
                path = directory / (name + ".c")
                path.write_text(translate(code, 0x1000, name), encoding="utf-8")
                paths.append(str(path))
            harness_path = directory / "test.c"
            harness_path.write_text(harness, encoding="utf-8")
            executable = directory / ("test.exe" if os.name == "nt" else "test")
            compiled = subprocess.run([CLANG, "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror",
                                       "-I", str(ROOT / "tests/prototype/include"), *paths, str(harness_path),
                                       "-o", str(executable)], capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    def test_moves_sp_zr_and_width(self):
        code = words(wide(0xffff, 0), wide(0x1234, 0, op=3, half=1),
                     wide(0, 1, op=0), wide(0xabcd, 1, bits=32),
                     arithmetic(31, 31, 16, subtract=True),
                     arithmetic(2, 31, 0),
                     0xaa1f03e3, 0xd65f03c0)
        self.compile_and_run({"moves": code}, '''
            memset(&c, 0, sizeof c); c.pc=0x1000; c.budget=100; c.sp=0x2000; c.x[30]=0xffff;
            CHECK(moves(&c)==HWR_RETURN); CHECK(c.pc==0xffff);
            CHECK(c.x[0]==0x1234ffff); CHECK(c.x[1]==0xabcd);
            CHECK(c.sp==0x1ff0); CHECK(c.x[2]==0x1ff0); CHECK(c.x[3]==0);
            CHECK(c.budget==92);
        ''')

    def test_flags_randomized_against_python(self):
        rng = random.Random(197)
        functions, checks = {}, []
        for bits in (32, 64):
            for subtract in (False, True):
                name = f"flags_{bits}_{int(subtract)}"
                functions[name] = words(register_arithmetic(0, 1, 2, bits, subtract, True), 0xd65f03c0)
                mask, sign = (1 << bits) - 1, 1 << (bits - 1)
                pairs = [(0, 0), (mask, 1), (sign - 1, 1), (sign, 1), (0, 1), (1, mask)]
                pairs += [(rng.getrandbits(64), rng.getrandbits(64)) for _ in range(64)]
                for aa, bb in pairs:
                    a, b = aa & mask, bb & mask
                    r = (a - b if subtract else a + b) & mask
                    carry = a >= b if subtract else a + b > mask
                    overflow = bool(((a ^ b) if subtract else ~(a ^ b)) & (a ^ r) & sign)
                    flags = (bool(r & sign) << 31) | ((r == 0) << 30) | (carry << 29) | (overflow << 28)
                    checks.append(f'''memset(&c,0,sizeof c); c.pc=0x1000; c.budget=4;
                        c.x[1]=UINT64_C({aa}); c.x[2]=UINT64_C({bb}); c.x[30]=0xffff;
                        CHECK({name}(&c)==HWR_RETURN); CHECK(c.x[0]==UINT64_C({r}));
                        CHECK(c.nzcv==UINT32_C({flags}));''')
        self.compile_and_run(functions, "\n".join(checks))

    def test_branch_loop_calls_and_external_yield(self):
        loop = words(wide(3), arithmetic(0, 0, 1, subtract=True, flags=True),
                     0x54ffffe1, 0xd65f03c0)  # b.ne back one instruction
        # BL +12; B +16; unreachable NOP; ADD x0,x0,#7; RET; RET x29.
        call = words(0x94000003, 0x14000004, 0xd503201f,
                     arithmetic(0, 0, 7), 0xd65f03c0, 0xd65f03a0)
        external = words(0x94000400)
        self.compile_and_run({"loop": loop, "call": call, "external": external}, '''
            memset(&c,0,sizeof c); c.pc=0x1000; c.budget=20; c.x[30]=0xffff;
            CHECK(loop(&c)==HWR_RETURN); CHECK(c.x[0]==0); CHECK(c.budget==12);
            memset(&c,0,sizeof c); c.pc=0x1000; c.budget=20; c.x[29]=0xffff;
            CHECK(call(&c)==HWR_RETURN); CHECK(c.x[0]==7); CHECK(c.x[30]==0x1004);
            memset(&c,0,sizeof c); c.pc=0x1000; c.budget=20;
            CHECK(external(&c)==HWR_EXTERNAL); CHECK(c.pc==0x2000); CHECK(c.x[30]==0x1004);
        ''')

    def test_memory_pairs_writeback_and_bounds(self):
        # STP x0,x1,[sp,#-16]!; LDP x2,x3,[sp],#16;
        # STRB w0,[x4,#1]; LDRB w5,[x4,#1]; RET.
        code = words(0xa9bf07e0, 0xa8c10fe2, 0x39000480, 0x39400485, 0xd65f03c0)
        self.compile_and_run({"memory_ops": code}, '''
            memset(&c,0,sizeof c); c.pc=0x1000; c.budget=20; c.x[30]=0xffff;
            c.memory=memory; c.memory_size=sizeof memory; c.guest_base=0x2000; c.sp=0x2080;
            c.x[0]=UINT64_C(0x8877665544332211); c.x[1]=0x12345678; c.x[4]=0x2000;
            CHECK(memory_ops(&c)==HWR_RETURN); CHECK(c.x[2]==c.x[0]); CHECK(c.x[3]==c.x[1]);
            CHECK(c.sp==0x2080); CHECK(c.x[5]==0x11); CHECK(memory[1]==0x11);
            c.pc=0x1000; c.sp=0x2008; c.budget=20;
            CHECK(memory_ops(&c)==HWR_MEMORY_FAULT); CHECK(c.pc==0x1000);
            CHECK(c.fault_address==0x1ff8); CHECK(c.sp==0x2008);
        ''')

    def test_budget_and_unmapped_pc(self):
        self.compile_and_run({"spin": words(0x14000000)}, '''
            memset(&c,0,sizeof c); c.pc=0x1000; c.budget=7;
            CHECK(spin(&c)==HWR_BUDGET_EXHAUSTED); CHECK(c.budget==0); CHECK(c.pc==0x1000);
            c.pc=0x1001; c.budget=7;
            CHECK(spin(&c)==HWR_EXTERNAL); CHECK(c.budget==7);
        ''')

    def test_pc_relative_and_shift_semantics(self):
        # ADR x0,+8; ADRP x1,+0x1000; ORR w2,wzr,w3,ASR #31;
        # ORR x4,xzr,x5,ROR #8; RET.
        code = words(0x10000040, 0xb0000001, 0x2a837fe2, 0xaac523e4, 0xd65f03c0)
        self.compile_and_run({"shifts": code}, '''
            memset(&c,0,sizeof c); c.pc=0x1000; c.budget=20; c.x[30]=0xffff;
            c.x[3]=0x80000000; c.x[5]=UINT64_C(0x0123456789abcdef);
            CHECK(shifts(&c)==HWR_RETURN); CHECK(c.x[0]==0x1008); CHECK(c.x[1]==0x2000);
            CHECK(c.x[2]==0xffffffff); CHECK(c.x[4]==UINT64_C(0xef0123456789abcd));
        ''')

    def test_all_condition_codes(self):
        checks = []
        for nzcv in range(16):
            n, z, carry, v = [(nzcv >> shift) & 1 for shift in (3, 2, 1, 0)]
            expected = [z, not z, carry, not carry, n, not n, v, not v,
                        carry and not z, not carry or z, n == v, n != v,
                        n == v and not z, n != v or z, True, True]
            checks.append(f"memset(&c,0,sizeof c); c.nzcv=UINT32_C({nzcv << 28});")
            checks.extend(f"CHECK(hwr_condition(&c,{condition})=={int(value)});"
                          for condition, value in enumerate(expected))
        self.compile_and_run({}, "\n".join(checks))

    def test_compare_test_and_indirect_branch(self):
        # CBZ w0,+8 must ignore x0's upper 32 bits; TBZ x1,#40,+8;
        # skipped MOVs leave x2 untouched. BR x3 leaves the region.
        code = words(0x34000040, wide(1, 2), 0xb6400041, wide(2, 2), 0xd61f0060)
        blr = words(0xd63f03c0)  # BLR x30 reads target before writing return address.
        self.compile_and_run({"branches": code, "indirect_call": blr}, '''
            memset(&c,0,sizeof c); c.pc=0x1000; c.budget=20;
            c.x[0]=UINT64_C(0x100000000); c.x[1]=0; c.x[2]=123; c.x[3]=0x3000;
            CHECK(branches(&c)==HWR_EXTERNAL); CHECK(c.pc==0x3000); CHECK(c.x[2]==123);
            memset(&c,0,sizeof c); c.pc=0x1000; c.budget=20; c.x[30]=0x3000;
            CHECK(indirect_call(&c)==HWR_EXTERNAL); CHECK(c.pc==0x3000); CHECK(c.x[30]==0x1004);
        ''')

    def test_unscaled_pre_post_and_address_wrap_fault(self):
        # STR x0,[x1],#8; LDUR x2,[x1,#-8]; LDR x3,[x1,#-8]!; RET.
        code = words(0xf8008420, 0xf85f8022, 0xf85f8c23, 0xd65f03c0)
        self.compile_and_run({"unscaled": code}, '''
            memset(&c,0,sizeof c); c.pc=0x1000; c.budget=20; c.x[30]=0xffff;
            c.memory=memory; c.memory_size=128; c.guest_base=0x2000;
            c.x[0]=UINT64_C(0xfacefeedcafebeef); c.x[1]=0x2020;
            CHECK(unscaled(&c)==HWR_RETURN); CHECK(c.x[1]==0x2020);
            CHECK(c.x[2]==c.x[0]); CHECK(c.x[3]==c.x[0]);
            c.guest_base=UINT64_MAX-3;
            CHECK(!hwr_memory_range(&c,UINT64_MAX-3,8)); CHECK(c.status==HWR_MEMORY_FAULT);
        ''')


if __name__ == "__main__":
    unittest.main()
