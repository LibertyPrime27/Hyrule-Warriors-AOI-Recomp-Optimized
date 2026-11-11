"""Real stage/shader memo functions, using synthetic immutable program inputs.

Lowering and translation providers count work requests; this tests cache
identity/invalidation, not shader semantics or measured game performance.
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


def source():
    draw = (ROOT / "runtime/vk_exec_draw.c").read_text(encoding="utf-8")
    shader_types = draw[draw.index("#define SHADER_SETS "):draw.index("// Registration keeps immutable")]
    stage_memo = draw[draw.index("#define STAGEM_N "):draw.index("// Texture and image types of a stage")]
    production = "\n".join((shader_types,
        function(draw, "static int program_stage_lookup("),
        function(draw, "static int find_stage_scan("), stage_memo,
        function(draw, "static const GfxShader *shader_for(")))
    prefix = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "gfx.h"
#include "nvn_program_bind.h"
#include "nvn_hmap.h"
#define NVN_PROG_MAGIC MAGIC('P','R','O','G')
#define NVN_STAGE_VS 0
#define NVN_STAGE_FS 1
#define NVN_STAGE_GS 2
#define NVN_STAGE_CS 5
#define W_SHADER 0
#define CHECK(x) do {if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
typedef struct {NvnProgramBindings programs;const prog_t *prog;} VkExec;
typedef struct {uint32_t id,stage,length;} Control;
static Control controls[4][6];
static uint8_t code[4][6][131072];
static uint64_t merged_serial=10000;
static unsigned lowered[4][6],translated[4][6],answer_variant;
static uint64_t translation_bytes,lowering_bytes,slot_serial;
struct GfxShader {unsigned id;};
static struct GfxShader shaders[4][6];
static int program_stage_info(const NVNshaderData *sd,uint32_t *stage,uint32_t *length) {
    if(!sd || !sd->control || !sd->data)return 0;
    const Control *c=sd->control;
    if(c->stage>=6)return 0;
    *stage=c->stage;*length=c->length;return 1;
}
static void program_stage_resolve(const prog_t *p,int stage,const uint8_t **ctl,const uint8_t **data,uint32_t *length) {
    const Control *c=(const Control *)*ctl;
    CHECK(p && c->id<4 && c->stage==(unsigned)stage && *data==code[c->id][stage] && *length==c->length);
    lowered[c->id][stage]++;lowering_bytes+=*length;
}
static void answers_for(VkExec *ex,int stage,const uint8_t *ctl,uint32_t mask,GfxShaderAnswers *a) {
    (void)ex;(void)stage;(void)ctl;(void)mask;memset(a,0,sizeof *a);a->tex_type[0]=(uint8_t)answer_variant;
}
static uint32_t slot_mask_of(uint64_t serial,int stage) {(void)stage;slot_serial=serial;return ~0u;}
static void slot_mask_learn(uint64_t serial,int stage,const GfxShader *shader) {
    (void)stage;CHECK(shader && serial==slot_serial);
}
static void warn_once(int key,const char *format,...) {(void)key;(void)format;CHECK(0);}
int vk_pipeline_async_on(void) {return 0;}
const GfxShader *gfx_shader_get(int stage,const uint8_t *ctl,const uint8_t *data,uint64_t length,
                              const uint8_t *c1,const GfxShaderAnswers *a) {
    const Control *c=(const Control *)ctl;(void)stage;CHECK(!c1 && c->id<4 && c->stage<6 && a->tex_type[0]==answer_variant);
    CHECK(data==code[c->id][c->stage] && length==c->length);
    translated[c->id][c->stage]++;translation_bytes+=length;return &shaders[c->id][c->stage];
}
const GfxShader *gfx_shader_get_async(int stage,const uint8_t *ctl,const uint8_t *data,uint64_t length,
                                    const GfxShaderAnswers *answers,int *pending) {
    *pending=0;return gfx_shader_get(stage,ctl,data,length,NULL,answers);
}
const GfxShader *gfx_shader_await(int stage,const uint8_t *ctl,const uint8_t *data,uint64_t length,const GfxShaderAnswers *answers) {
    return gfx_shader_get(stage,ctl,data,length,NULL,answers);
}
'''
    body = r'''
static uint64_t new_serial(void) {return ++merged_serial;}
static prog_t program(unsigned id,unsigned mask,uint64_t serial) {
    prog_t p={.magic=NVN_PROG_MAGIC,.serial=serial};
    for(unsigned stage=0;stage<6;stage++)if(mask&(1u<<stage)) {
        controls[id][stage]=(Control){id,stage,stage==NVN_STAGE_CS?107264:4096};
        p.sd[p.nstage++]=(NVNshaderData){(uintptr_t)code[id][stage],&controls[id][stage]};
    }
    return p;
}
static void bind(VkExec *ex,const prog_t *p,unsigned mask) {
    CHECK(nvn_program_bind(&ex->programs,p,mask));
    CHECK(nvn_program_refresh(&ex->programs,program_stage_lookup,new_serial)>=0);
    ex->prog=ex->programs.mask?&ex->programs.merged:NULL;
}
static void get(VkExec *ex,int stage,unsigned id) {
    int present=0,pending=0;
    const GfxShader *s=shader_for(ex,stage,&present,&pending,0);
    CHECK(s==&shaders[id][stage] && present && !pending);
    CHECK(slot_serial==ex->programs.source[stage]->serial);
    const uint8_t *ctl,*data;uint32_t length;
    CHECK(bound_stage(ex,stage,&ctl,&data,&length));
    CHECK(ctl==(const uint8_t *)&controls[id][stage] && data==code[id][stage] && length==controls[id][stage].length);
}
int main(void) {
    VkExec ex={0};prog_t a=program(0,33,10),b=program(1,1,20),fs=program(2,2,30);
    bind(&ex,&a,33);bind(&ex,&fs,2);
    get(&ex,0,0);get(&ex,1,2);get(&ex,5,0);
    uint64_t first_combination=ex.prog->serial;
    for(unsigned i=0;i<10000;i++) {
        unsigned id=(i&1)?0:1;
        bind(&ex,id?&b:&a,1);
        CHECK(ex.prog->serial>first_combination && ex.prog->serial!=a.serial && ex.prog->serial!=b.serial);
        get(&ex,0,id);get(&ex,1,2);get(&ex,5,0);
    }
    CHECK(lowered[0][0]==1 && lowered[1][0]==1 && lowered[2][1]==1 && lowered[0][5]==1);
    CHECK(translated[0][0]==1 && translated[1][0]==1 && translated[2][1]==1 && translated[0][5]==1);
    CHECK(a.serial==10 && b.serial==20 && fs.serial==30); // binding never changes source identities
    // Changing only FS invalidates its stage, not the unchanged VS and CS.
    fs.serial=31;
    CHECK(!nvn_program_stage_source(&ex.programs,1));
    bind(&ex,&fs,2);get(&ex,1,2);get(&ex,0,0);get(&ex,5,0);
    CHECK(lowered[2][1]==2 && translated[2][1]==2 && lowered[0][0]==1 && lowered[0][5]==1);
    // Descriptor-answer variants still select distinct translations.
    answer_variant=1;get(&ex,0,0);get(&ex,0,0);
    CHECK(lowered[0][0]==1 && translated[0][0]==2);
    answer_variant=0;get(&ex,0,0);CHECK(translated[0][0]==2);
    // SetShaders/reinitialization at the same source address receives a new serial.
    a.serial=11;
    CHECK(!nvn_program_stage_source(&ex.programs,0) && !nvn_program_stage_source(&ex.programs,5));
    bind(&ex,&a,33);get(&ex,0,0);get(&ex,5,0);
    CHECK(lowered[0][0]==2 && lowered[0][5]==2 && translated[0][0]==3 && translated[0][5]==2);
    // Finalization invalidates source lookup even before another refresh. A
    // malformed finalized binding must still fail lookup, not hit shader caches.
    a.serial=0;a.magic=0;
    CHECK(!program_stage_source(&ex,0) && !program_stage_source(&ex,5));
    int present=1,pending=1;
    CHECK(!shader_for(&ex,0,&present,&pending,0) && !present && !pending);
    CHECK(nvn_program_refresh(&ex.programs,program_stage_lookup,new_serial)==-1);
    a.serial=12;a.magic=NVN_PROG_MAGIC;
    bind(&ex,&a,33);get(&ex,0,0);get(&ex,5,0);
    CHECK(lowered[0][0]==3 && lowered[0][5]==3 && translated[0][0]==4 && translated[0][5]==3);
    // An explicit unbind clears just those stages; unrelated FS remains usable.
    bind(&ex,NULL,33);CHECK(!program_stage_source(&ex,0) && !program_stage_source(&ex,5));get(&ex,1,2);
    CHECK(!shader_for(&ex,0,&present,&pending,0) && !present && !pending);
    CHECK(!program_stage_source(&ex,-1) && !program_stage_source(&ex,6));
    CHECK(!nvn_program_stage_source(NULL,0));
    // Standalone direct-program probes retain the uncached-serial-zero seam.
    prog_t direct=program(3,1,0);VkExec direct_ex={.prog=&direct};
    unsigned before=translated[3][0];
    CHECK(shader_for(&direct_ex,0,&present,&pending,0)==&shaders[3][0]);
    CHECK(shader_for(&direct_ex,0,&present,&pending,0)==&shaders[3][0]);
    CHECK(translated[3][0]==before+2 && lowered[3][0]==2);
    printf("stage source memo: alternating 10000 bindings, serial/answer/finalize invalidation passed; lowering bytes %llu translation bytes %llu\n",
           (unsigned long long)lowering_bytes,(unsigned long long)translation_bytes);
    return 0;
}
'''
    return prefix + production + body


@unittest.skipUnless(CC.is_file() and os.name == "nt", "existing F-drive Windows C compiler required")
class ShaderSourceMemo(unittest.TestCase):
    def test_alternating_bindings_and_lifetime_invalidation(self):
        folder = ROOT / "build/test/shader-source-memo"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temporary:
            temporary = Path(temporary).resolve()
            self.assertTrue(temporary.is_relative_to(folder.resolve()))
            path, exe = temporary / "probe.c", temporary / "probe.exe"
            path.write_text(source(), encoding="utf-8")
            build = subprocess.run([str(CC), "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                                    "-I", str(ROOT / "runtime"), str(path), "-o", str(exe)],
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("alternating 10000 bindings", run.stdout)


if __name__ == "__main__":
    unittest.main()
