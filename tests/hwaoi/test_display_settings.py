"""Independent decoding of native-size patches and strict preset/refusal checks."""
import os
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "recomp"))
import hwaoi
import patches


class DisplaySettingsTests(unittest.TestCase):
    def table(self):
        return patches.load(ROOT / "recomp/patches-hwaoi196608-display.txt",
                            [("main", 0x7000000000, 0x29a0000), ("sdk", 0x7002c00000, 0xff0000)])

    def test_hash_and_every_displaced_instruction_are_required(self):
        table = self.table()
        words = {off: row[2] for off, row in table.sites["main"].items()}
        class Module:
            sha256 = json.loads(hwaoi.PROFILE.read_text())["modules"]["main"]
            def code(self, address, length):
                return struct.pack("<I", words[address])
        module = Module()
        with patch("hwrecomp.nso.Nso", return_value=module):
            self.assertTrue(hwaoi.display_profile(Path("unused")).is_file())
            for address in words:
                words[address] ^= 1
                with self.assertRaisesRegex(ValueError, "instruction mismatch"):
                    hwaoi.display_profile(Path("unused"))
                words[address] ^= 1
            module.sha256 = "0" * 64
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                hwaoi.display_profile(Path("unused"))

    def test_page_loads_and_region_depth(self):
        from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
        dis = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
        dis.detail = True
        table = self.table()
        rows = table.for_module("main", 0x7000000000)
        def instructions(off):
            # Every replacement is lifted at the original PC by the generator.
            return [next(dis.disasm(struct.pack("<I", w), 0x7000000000 + off))
                    for w in rows[0x7000000000 + off][1]]
        expected = {0x1af1cf4: ("x9", 0x80), 0x1af34a4: ("x9", 0x80),
                    0x1af1b20: ("x10", 0x88), 0x1af1cf0: ("x8", 0x88),
                    0x1af3318: ("x8", 0x88), 0x1af332c: ("x8", 0x88),
                    0x1af3338: ("w8", 0x8c), 0x1af4170: ("w3", 0x88),
                    0x1af4174: ("w4", 0x8c), 0x1e62d68: ("w1", 0x88),
                    0x1e62d6c: ("w2", 0x8c), 0x1e62d8c: ("w1", 0x88),
                    0x1e62d90: ("w2", 0x8c), 0x1e63780: ("x8", 0x88), 0x19eb098: ("x12", 0x98),
                    0x1af1af8: ("x9", 0xa0)}
        for off, (register, offset) in expected.items():
            first, load, *_ = instructions(off)
            self.assertEqual(first.mnemonic, "adrp")
            self.assertEqual(first.operands[1].imm, table.page)
            self.assertEqual(load.mnemonic, "ldr")
            self.assertEqual(load.reg_name(load.operands[0].reg), register)
            self.assertEqual(load.operands[1].mem.disp, offset)
            self.assertEqual(load.reg_name(load.operands[1].mem.base), "x" + register[1:])
        shift = instructions(0x1af332c)[2]
        self.assertEqual((shift.mnemonic, shift.operands[2].imm), ("lsl", 32))
        self.assertNotIn(0x1af3348, table.sites["main"])  # retain depth=1, not packed height
        for off in (0x1af1d00, 0x1af34b0, 0x1af1b28, 0x1af1cfc, 0x1af3324, 0x1e6378c, 0x19eb09c,
                    0x1af1afc, 0x1af1b00):
            self.assertEqual(instructions(off)[0].mnemonic, "nop")

    def test_native_presets_and_invalid_forms(self):
        cc = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
        if not cc.is_file():
            self.skipTest("existing native compiler unavailable")
        source = r'''
#include <assert.h>
#include "hwaoi_display.h"
int main(void) {
 const char *names[]={"1280x720","1920x1080","2560x1440","3840x2160"};
 unsigned widths[]={1280,1920,2560,3840}, heights[]={720,1080,1440,2160};
 unsigned size[2];
 for(unsigned i=0;i<4;i++) { assert(hwaoi_display_size(names[i],size));
  assert(size[0]==widths[i] && size[1]==heights[i]); }
 const char *bad[]={0,"","1920X1080","1920x1080extra","3840x2161","5120x1440","-1920x1080"," 1920x1080"};
 for(unsigned i=0;i<sizeof bad/sizeof *bad;i++) {size[0]=111;size[1]=222;
  assert(!hwaoi_display_size(bad[i],size));assert(size[0]==111&&size[1]==222);}
 unsigned output[2],scene[2];uint64_t child,parent,extra;
 assert(hwaoi_display_dimensions("3840x2160",NULL,output,scene));
 assert(hwaoi_display_pool_plan(scene,NULL,&child,&parent,&extra));
 assert(child==0x160000000ull&&parent==0x178b05008ull&&extra==0x3d200000ull);
 assert(hwaoi_display_dimensions("3840x2160","1920x1080",output,scene));
 assert(hwaoi_display_pool_plan(scene,NULL,&child,&parent,&extra));
 assert(child==0x122f00000ull&&parent==0x13ba05008ull&&!extra);
 assert(!hwaoi_display_dimensions("1920x1080","3840x2160",output,scene));
 for(unsigned i=0;i<3;i++) {
  assert(hwaoi_display_dimensions(names[i],NULL,output,scene));
  assert(hwaoi_display_pool_plan(scene,NULL,&child,&parent,&extra));assert(!extra&&child==0x122f00000ull);
 }
 assert(hwaoi_display_pool_plan(scene,"1",&child,&parent,&extra)&&extra==0x5d200000ull);
 scene[0]=3840;scene[1]=2160;
 assert(hwaoi_display_pool_plan(scene,"0",&child,&parent,&extra)&&!extra);
 assert(hwaoi_display_pool_plan(scene,"5120",&child,&parent,&extra));
 assert(child==0x140000000ull&&parent==0x158b05008ull&&extra==0x1d200000ull);
 const char *invalid[]={"","2","yes","+1"};
 for(unsigned i=0;i<4;i++){child=1;parent=2;extra=3;assert(!hwaoi_display_pool_plan(scene,invalid[i],&child,&parent,&extra));
  assert(child==1&&parent==2&&extra==3);}
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "display.c"; path.write_text(source)
            exe = path.with_suffix(".exe")
            subprocess.run([str(cc), "-O2", "-I", str(ROOT / "runtime"), str(path), "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True)

    def test_application_growth_is_profile_scoped(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from nvn2_compute_c0_test import function
        policy = function((ROOT / "runtime/resolution.c").read_text(encoding="utf-8"),
                          "uint64_t res_heap_extra_bytes(")
        source = r'''
#include <assert.h>
#include <stdlib.h>
#include "hwaoi_display.h"
#define RT_TARGET_HWAOI 1
static const char *resolution="1920x1080",*native,*experiment,*scene_override;
static int profile=1,foreign_heap_reads;
static int rt_patch_set(const char *name){assert(!strcmp(name,"hwaoi_display"));return profile?0:-1;}
static const char *cfg_value(const char *name){assert(!strcmp(name,"TOTK_OUTPUT_RES"));return resolution;}
static const char *cfg_get(const char *name){
 if(!strcmp(name,"TOTK_NATIVE_1080"))return native;
 if(!strcmp(name,"TOTK_SCENE_RES"))return scene_override;
 assert(!strcmp(name,"TOTK_DISPLAY_POOL_TEST"));return experiment;
}
static void rt_fatal(const char *fmt,...){(void)fmt;exit(73);}
static uint64_t res_heap_extra_kib(void){foreign_heap_reads++;return 42;}
''' + policy + r'''
int main(int argc,char **argv){
 assert(argc==2);if(!strcmp(argv[1],"bad")){resolution="3840x2161";res_heap_extra_bytes();return 1;}
 assert(!res_heap_extra_bytes());resolution="3840x2160";assert(res_heap_extra_bytes()==0x3d200000ull);
 scene_override="1920x1080";assert(!res_heap_extra_bytes());scene_override=NULL;
 native="0";assert(!res_heap_extra_bytes());native=NULL;profile=0;assert(!res_heap_extra_bytes());
 assert(!foreign_heap_reads);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capacity.c"; path.write_text(source)
            exe = path.with_suffix(".exe")
            subprocess.run([os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe", "-O2", "-I", str(ROOT / "runtime"),
                            str(path), "-o", str(exe)], check=True, capture_output=True)
            subprocess.run([str(exe), "good"], check=True)
            self.assertEqual(subprocess.run([str(exe), "bad"], capture_output=True).returncode, 73)

    def test_original_child_and_parent_capacities_match_observed_pools(self):
        from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
        dis = Cs(CS_ARCH_ARM64, CS_MODE_ARM); dis.detail = True
        table = self.table()
        def wide_constant(offsets):
            value = 0
            for off in offsets:
                word = table.sites["main"][off][2]
                ins = next(dis.disasm(struct.pack("<I", word), off))
                if ins.mnemonic == "mov":
                    value = ins.operands[1].imm
                else:
                    self.assertEqual(ins.mnemonic, "movk")
                    shift = ins.operands[1].shift.value
                    value = value & ~(0xffff << shift) | ins.operands[1].imm << shift
            return value
        child = wide_constant((0x19eb098, 0x19eb09c))
        parent = wide_constant((0x1af1af8, 0x1af1afc, 0x1af1b00))
        self.assertEqual(child, 4655 * 1024 * 1024)
        self.assertEqual(parent, (4655 + 45 + 350) * 1024 * 1024 + 0x5008)
        self.assertEqual(parent - child, 395 * 1024 * 1024 + 0x5008)


if __name__ == "__main__":
    unittest.main()
