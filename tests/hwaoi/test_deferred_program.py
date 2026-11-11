"""Actual registration and stage-resolution functions with synthetic owned data.

No game bytes, GPU, or shader semantics are mocked as correctness evidence:
the lowerer spy verifies timing, stage selection and pointer/length propagation.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function
import geometry_program_control_test


def stage_source():
    shader = (ROOT / "runtime/gfx_shader.c").read_text()
    draw = (ROOT / "runtime/vk_exec_draw.c").read_text()
    functions = [function(shader, "int gfx_shader_is_nvn2("),
                 function(draw, "static int program_stage_info("),
                 function(draw, "static int program_stage_lookup("),
                 function(draw, "static void program_stage_resolve("),
                 function(draw, "static int find_stage_scan(")]
    memo = draw[draw.index("#define STAGEM_N "):draw.index("static int find_stage(")]
    functions += [memo, function(draw, "static int find_stage(")]
    preamble = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "gfx.h"
const uint8_t *nvn_program_source(const uint8_t *p) {return p;}
#define NVN_PROG_MAGIC MAGIC('P','R','O','G')
#define CHECK(x) do { if(!(x)) { fprintf(stderr,"check line %d: %s\n",__LINE__,#x);exit(1); } } while(0)
static uint8_t raw_ctl[6][0xd8],raw_code[6][0xa0],legacy[6][0x880],lower_code[6][0x100];
static unsigned calls[6], fail_stage=99, dumps;
static uint32_t u32(const void *p) {uint32_t n;memcpy(&n,p,4);return n;}
static void put(void *p,uint32_t n) {memcpy(p,&n,4);}
void rt_fatal(const char *format,...) {(void)format;if(fail_stage<6)CHECK(dumps==1);exit(73);}
void gfx_shader_nvn2_dump_failure(const uint8_t *control,const uint8_t *code) {
    CHECK(fail_stage<6 && control==raw_ctl[fail_stage] && code==raw_code[fail_stage]);dumps++;
}
uint64_t gfx_shader_nvn2_hash(const uint8_t *control,const uint8_t *code) {(void)control;return u32(code);}
int nvn2_lower(const uint8_t *control,const uint8_t *code,const uint8_t **out_ctl,const uint8_t **out_code) {
    unsigned stage=99;
    for(unsigned i=0;i<6;i++)if(control==raw_ctl[i] && code==raw_code[i])stage=i;
    CHECK(stage<6);calls[stage]++;
    if(stage==fail_stage)return 0;
    *out_ctl=legacy[stage];*out_code=lower_code[stage];return 1;
}
'''
    body = r'''
int main(int argc,char **argv) {
    static const unsigned sph_type[6]={1,5,4,2,3,0};
    prog_t p={.magic=NVN_PROG_MAGIC,.serial=51,.nstage=6};
    for(unsigned i=0;i<6;i++) {
        put(raw_ctl[i],0x98761234);put(raw_ctl[i]+4,0xc9);put(raw_ctl[i]+0x38,0xa0);
        put(raw_code[i],i==5?0:1|(4<<5)|(sph_type[i]<<10));
        put(legacy[i]+0x704,0x100);put(legacy[i]+0x714,i);
        p.sd[i]=(NVNshaderData){(uintptr_t)raw_code[i],raw_ctl[i]};
    }
    for(unsigned i=0;i<6;i++) {
        NVNshaderData out;CHECK(program_stage_lookup(&p,i,&out)==1);
        CHECK(out.control==raw_ctl[i] && out.data==(uintptr_t)raw_code[i] && !calls[i]);
    }
    const uint8_t *ctl,*code;uint32_t size;
    if(argc>1 && !strcmp(argv[1],"failure")) {
        fail_stage=1;find_stage(&p,1,&ctl,&code,&size);return 2;
    }
    CHECK(find_stage(&p,1,&ctl,&code,&size));
    CHECK(ctl==legacy[1] && code==lower_code[1] && size==0x100 && calls[1]==1);
    for(unsigned i=0;i<6;i++)if(i!=1)CHECK(!calls[i]);
    CHECK(find_stage(&p,1,&ctl,&code,&size) && calls[1]==1);
    CHECK(find_stage(&p,5,&ctl,&code,&size) && calls[5]==1 && size==0x100);
    // The source identity changes even when its address and stage list do not.
    p.serial++;
    CHECK(find_stage(&p,1,&ctl,&code,&size) && calls[1]==2);
    // Legacy input bypasses lowering, and a missing stage does not lower others.
    p.serial++;p.nstage=1;p.sd[0]=(NVNshaderData){(uintptr_t)lower_code[0],legacy[0]};
    CHECK(find_stage(&p,0,&ctl,&code,&size) && ctl==legacy[0] && !calls[0]);
    CHECK(!find_stage(&p,1,&ctl,&code,&size) && calls[1]==2);
    p.serial=0;CHECK(find_stage(&p,0,&ctl,&code,&size) && size==0x100);
    p.sd[0].control=NULL;CHECK(program_stage_lookup(&p,0,(NVNshaderData *)&p.sd[1])==-1);
    CHECK(!gfx_shader_is_nvn2(NULL));
    puts("deferred stage resolution passed");return 0;
}
'''
    return preamble + "\n".join(functions) + body


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Windows Clang required")
class DeferredProgramTests(unittest.TestCase):
    def test_registration_and_deferred_stage_resolution(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-deferred-") as tmp:
            directory = Path(tmp)
            for name, source in (("lifetime", geometry_program_control_test.source()), ("stages", stage_source())):
                path = directory / (name + ".c")
                path.write_text(source, encoding="utf-8")
                for opt in ("-O0", "-O2"):
                    exe = directory / (name + opt + ".exe")
                    result = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "runtime"),
                                             str(path), "-o", str(exe)], capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    if name == "stages":
                        failed = subprocess.run([str(exe), "failure"], capture_output=True, text=True, timeout=30)
                        self.assertEqual(failed.returncode, 73, failed.stdout + failed.stderr)


if __name__ == "__main__":
    unittest.main()
