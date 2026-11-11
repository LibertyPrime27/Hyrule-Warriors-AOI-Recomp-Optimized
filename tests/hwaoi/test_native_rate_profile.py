"""Exact-build refusal and execution of the production scheduler-rate selector."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "recomp")]
import hwaoi
import patches
import gen
from lifter import Lifter


class NativeRateProfileTests(unittest.TestCase):
    def table(self):
        return patches.load(ROOT / "recomp/patches-hwaoi196608-rate.txt",
                            [("main", 0x7000000000, 0x29a0000)])

    def test_exact_main_and_instruction_required(self):
        words = {off: row[2] for off, row in self.table().sites["main"].items()}
        class Module:
            sha256 = json.loads(hwaoi.PROFILE.read_text())["modules"]["main"]
            def code(self, address, size):
                assert size == 4
                return struct.pack("<I", words[address])
        module = Module()
        with patch("hwrecomp.nso.Nso", return_value=module):
            self.assertTrue(hwaoi.rate_profile(Path("unused")).is_file())
            for address in words:
                words[address] ^= 1
                with self.assertRaisesRegex(ValueError, "instruction mismatch"):
                    hwaoi.rate_profile(Path("unused"))
                words[address] ^= 1
            module.sha256 = "0" * 64
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                hwaoi.rate_profile(Path("unused"))

    def test_decoded_selector_uses_no_flags_and_stays_opt_in(self):
        from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
        table = self.table()
        self.assertEqual(table.sets, [("hwaoi_rate", "TOTK_RATE_UNLOCK", 0), ("hwaoi_step", "TOTK_RATE_UNLOCK", 0)])
        pc = 0x70006c281c
        _, words = table.for_module("main", 0x7000000000)[pc]
        dis = Cs(CS_ARCH_ARM64, CS_MODE_ARM); dis.detail = True
        ins = [next(dis.disasm(struct.pack("<I", word), pc)) for word in words]
        self.assertEqual([i.mnemonic for i in ins], ["ldr", "adrp", "ldr", "eor", "clz", "lsr", "neg", "eor", "and", "eor"])
        self.assertTrue(all(not i.update_flags for i in ins))
        self.assertEqual(ins[0].reg_name(ins[0].operands[1].mem.base), "x27")
        self.assertEqual(ins[0].operands[1].mem.disp, 0x30)
        self.assertEqual(ins[1].operands[1].imm, table.page)
        self.assertEqual(ins[2].operands[1].mem.disp, 0xb0)
        self.assertEqual(ins[3].operands[2].imm, 60)
        self.assertEqual(ins[5].operands[2].imm, 5)

    @unittest.skipUnless(os.name == "nt", "Windows native semantic fixture")
    def test_lifted_selector_exhaustive_inputs_and_preserved_state(self):
        table = self.table(); pc = 0x70006c281c
        class Module:
            lifter = Lifter(0x7000000000, "c")
            def lift(self, address):
                return self.lifter.lift(0xb9403368 if address == pc else 0xd65f03c0, address)
        emitter = gen.Emitter(Module(), {pc}, patched=table.for_module("main", 0x7000000000))
        lines, refs = emitter.function(pc, pc + 8)
        self.assertFalse(refs)
        source = r'''
#include <windows.h>
#include <assert.h>
#include <stdio.h>
#include "cpu.h"
#include "simd.h"
volatile int g_fnprof=0;uint8_t rt_patch_on[1];
void fnprof_hit(uint64_t a,uint64_t b,uint64_t d){(void)a;(void)b;(void)d;}
''' + "\n".join(lines) + r'''
static unsigned checks;
static uint8_t node[64] __attribute__((aligned(16)));
static void check(uint32_t original,uint32_t target,int enabled){
 Cpu c,before;memset(&c,0xa5,sizeof c);c.x[27]=(uintptr_t)node;c.nzcv=0x90000000;
 memcpy(node+0x30,&original,4);*(uint32_t*)(uintptr_t)0x70029a00b0ull=target;
 rt_patch_on[0]=(uint8_t)enabled;before=c;f_70006c281c(&c);
 assert(c.x[8]==(enabled&&original==60?target:original));
 for(unsigned i=0;i<32;i++)if(i!=8&&(!enabled||(i!=9&&i!=10)))assert(c.x[i]==before.x[i]);
 assert(c.sp==before.sp&&c.pc==before.pc&&c.nzcv==before.nzcv);
 assert(c.fpcr==before.fpcr&&c.fpsr==before.fpsr&&!memcmp(c.v,before.v,sizeof c.v));checks++;
}
int main(void){
 void *page=VirtualAlloc((void*)(uintptr_t)0x70029a0000ull,4096,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);assert(page);
 const uint32_t rates[]={30,60,90,120},edges[]={0x7fffffff,0x80000000,0xffffffc3,0xffffffff};
 for(unsigned r=0;r<4;r++)for(int enabled=0;enabled<2;enabled++){
  for(uint32_t original=0;original<65536;original++)check(original,rates[r],enabled);
  for(unsigned i=0;i<4;i++)check(edges[i],rates[r],enabled);
 }
 assert(checks==524320);VirtualFree(page,0,MEM_RELEASE);puts("524320 native selector cases passed");
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rate.c"; path.write_text(source)
            for opt in ("-O0", "-O2"):
                exe = path.with_suffix(".exe")
                built = subprocess.run([os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe", opt, "-march=x86-64-v3", "-I", str(ROOT / "runtime"),
                                        str(path), "-o", str(exe)], capture_output=True, text=True)
                self.assertEqual(built.returncode, 0, built.stderr)
                run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertIn("524320 native selector cases passed", run.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows native semantic fixture")
    def test_inherited_and_fixed_steps_preserve_time_scale_and_live_state(self):
        table = self.table(); base = 0x7000000000
        functions = []
        for offset in (0x6c2b50, 0x6c2b80):
            pc = base + offset
            word = table.sites["main"][offset][2]
            class Module:
                lifter = Lifter(base, "c")
                def lift(self, address):
                    return self.lifter.lift(word if address == pc else 0xd65f03c0, address)
            emitter = gen.Emitter(Module(), {pc}, patched=table.for_module("main", base))
            lines, refs = emitter.function(pc, pc + 8)
            self.assertFalse(refs)
            functions.extend(lines)
        source = r'''
#include <windows.h>
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include "cpu.h"
#include "simd.h"
volatile int g_fnprof=0;uint8_t rt_patch_on[2];
void fnprof_hit(uint64_t a,uint64_t b,uint64_t d){(void)a;(void)b;(void)d;}
''' + "\n".join(functions) + r'''
static unsigned checks;
static uint32_t bits(float f){uint32_t u;memcpy(&u,&f,4);return u;}
static void check(uint32_t original,float seconds,float parent,int enabled,int inherited){
 Cpu c,before;uint8_t node[64]={0},owner[64]={0};memset(&c,0xa5,sizeof c);
 c.x[8]=(uintptr_t)owner;c.x[10]=(uintptr_t)node;c.nzcv=0x90000000;c.fpcr=0;c.fpsr=0;
 c.x[26]=31250000;c.x[28]=1000000000;c.x[0]=c.x[28]+(uint64_t)(seconds*c.x[26]);
 memcpy(node+0x30,&original,4);memcpy(owner+0x3c,&parent,4);
 float hz=original>1?(float)original:1.0f;
 c.v[0]=bits(hz);c.v[1]=bits(1.0f);
 float elapsed=(float)((double)(c.x[0]-c.x[28])/(double)c.x[26]);
 if(elapsed>1.0f/15)elapsed=1.0f/15;
 float ratio=elapsed*60.0f;
 rt_patch_on[1]=(uint8_t)enabled;before=c;
 if(inherited)f_70006c2b50(&c);else f_70006c2b80(&c);
 float expected=inherited?parent:1.0f/hz;
 if(enabled&&original==60)expected=inherited?parent*ratio:elapsed;
 if((uint32_t)c.v[0]!=bits(expected))fprintf(stderr,"step mismatch: hz=%u seconds=%.9g parent=%.9g enabled=%d inherited=%d actual=%08x expected=%08x\n",original,seconds,parent,enabled,inherited,(uint32_t)c.v[0],bits(expected));
 assert((uint32_t)c.v[0]==bits(expected));assert(!(c.v[0]>>32));
 for(unsigned i=0;i<32;i++){
  if(!enabled||(i!=8&&i!=12&&i!=13))assert(c.x[i]==before.x[i]);
  if(i!=0&&(!enabled||(i!=1&&i!=2)))assert(c.v[i]==before.v[i]);
 }
 assert(c.sp==before.sp&&c.pc==before.pc&&c.nzcv==before.nzcv);
 assert(c.fpcr==before.fpcr&&c.fpsr==before.fpsr);checks++;
}
int main(void){
 void *page=VirtualAlloc((void*)(uintptr_t)0x70029a0000ull,4096,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);assert(page);
 const float seconds[]={1.0f/30,1.0f/60,1.0f/90,1.0f/120,.011f,1.0f/15,0,.25f,1};
 const float scales[]={0,.25f,.5f,1,2,-1};
 const uint32_t edges[]={0x7fffffff,0x80000000,0xffffffc3,0xffffffff};
 for(unsigned t=0;t<9;t++)for(unsigned s=0;s<6;s++)for(int enabled=0;enabled<2;enabled++)for(int inherited=0;inherited<2;inherited++){
  for(uint32_t hz=0;hz<1024;hz++)check(hz,seconds[t],scales[s]/60.0f,enabled,inherited);
  for(unsigned e=0;e<4;e++)check(edges[e],seconds[t],scales[s]/60.0f,enabled,inherited);
 }
 VirtualFree(page,0,MEM_RELEASE);printf("%u independent step cases passed\n",checks);return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "step.c"; path.write_text(source)
            for opt in ("-O0", "-O2"):
                exe = path.with_suffix(".exe")
                built = subprocess.run([os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe", opt, "-march=x86-64-v3", "-I", str(ROOT / "runtime"),
                                        str(path), "-o", str(exe)], capture_output=True, text=True)
                self.assertEqual(built.returncode, 0, built.stderr)
                result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("222048 independent step cases passed", result.stdout)

    def test_incomplete_or_stale_generation_cannot_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp); inputs = out / "hwaoi-input.json"; inputs.write_text('{"profile":"fixture"}')
            with self.assertRaisesRegex(ValueError, "missing"):
                hwaoi.require_successful_generation(out)
            record = {"status": "generating", "input_sha256": hwaoi.sha256(inputs)}
            (out / "hwaoi-generation.json").write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                hwaoi.require_successful_generation(out)
            record["status"] = "pass"; (out / "hwaoi-generation.json").write_text(json.dumps(record))
            hwaoi.require_successful_generation(out)
            inputs.write_text('{"profile":"different"}')
            with self.assertRaisesRegex(ValueError, "different inputs"):
                hwaoi.require_successful_generation(out)


if __name__ == "__main__":
    unittest.main()
