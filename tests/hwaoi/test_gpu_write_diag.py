"""Original CPU fixtures for passive large-buffer write diagnostics.

The real diagnostic helpers and public mirror-mark function are extracted from
production source. Vulkan handles remain inert tokens; no GPU or game is run.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
DEPS = Path("F:/TotKDependencies/runtime/deps/include")
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function


def source():
    draw = (ROOT / "runtime/vk_exec_draw.c").read_text(encoding="utf-8")
    memory = (ROOT / "runtime/vk_memory.c").read_text(encoding="utf-8")
    helpers = "\n".join((function(draw, "static void gpu_write_binding_diag("),
                          function(memory, "static void mirror_write_diag("),
                          function(memory, "void vk_mem_gpu_wrote(")))
    return r'''
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "vk_exec.h"
#define CHECK(x) do {if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
#define LARGE (UINT64_C(16)<<20)
enum {MS_ON=1};
// Private pool layout is irrelevant to this CPU fixture: only the production
// functions' named fields are used. No real pool is cast to this test type.
typedef struct PoolRec {
 uint64_t storage_serial,lo,size,import_lo,import_hi;
 unsigned mstate;VkBuffer mbuf;
} PoolRec;
typedef struct {PoolRec *r;uint64_t off,size;} MRange;
static MRange *wbl,last_range;static int nwbl,cwbl;
static unsigned descriptor_lines,mirror_lines,descriptor_limits,mirror_limits;
static unsigned config_reads,stage_reads,origin_reads,hash_reads,range_calls;
static unsigned vertex_invalidations;
void vk_vertex_cache_buffer_written(const VkMemSpan *span,uint64_t size) {CHECK(span && size);vertex_invalidations++;}
static const char *setting;
static char last_log[2048];
const char *cfg_get(const char *key) {CHECK(!strcmp(key,"TOTK_GPU_WRITE_DIAG"));config_reads++;return setting;}
void rt_log(const char *fmt,...) {
 va_list args;va_start(args,fmt);vsnprintf(last_log,sizeof last_log,fmt,args);va_end(args);
 if(strstr(last_log,"descriptor access=declared-writable"))descriptor_lines++;
 else if(strstr(last_log,"mirror mark=potential-writeback"))mirror_lines++;
 else if(strstr(last_log,"descriptor observation limit"))descriptor_limits++;
 else if(strstr(last_log,"mirror observation limit"))mirror_limits++;
 else CHECK(0);
}
static int bound_stage(const VkExec *ex,int st,const uint8_t **ctl,const uint8_t **code,uint32_t *len) {
 const prog_t *p=ex->prog;
 static const uint8_t control[16]={1},native[16]={2};
 CHECK(p && st>=0 && st<6);stage_reads++;*ctl=control;*code=native;*len=16;return 1;
}
uint64_t nvn2_origin(const uint8_t *code) {CHECK(code && code[0]==2);origin_reads++;return 0;}
int gfx_shader_is_nvn2(const uint8_t *control) {CHECK(control && control[0]==1);return 1;}
uint64_t gfx_shader_nvn2_hash(const uint8_t *control,const uint8_t *code) {
 CHECK(control[0]==1 && code[0]==2);hash_reads++;return UINT64_C(0xabcdef1234);
}
uintptr_t plat_exe_base(void) {return 0x1000;}
static void range_add(MRange **ranges,int *count,int *capacity,PoolRec *r,uint64_t off,uint64_t bytes) {
 CHECK(ranges==&wbl && count==&nwbl && capacity==&cwbl);
 range_calls++;last_range=(MRange){r,off,bytes};(*count)++;
}
''' + helpers + r'''
int main(int argc,char **argv) {
 CHECK(argc==2);setting=!strcmp(argv[1],"unset")?NULL:argv[1];int on=setting && !strcmp(setting,"1");
 VkDescriptorBufferInfo descriptor={.buffer=(VkBuffer)(uintptr_t)1,.offset=256,.range=LARGE-1};
 // Small observations must not even consult configuration or dereference
 // unrelated stage/pool inputs.
 gpu_write_binding_diag(NULL,5,NULL,NULL,&descriptor,0);
 mirror_write_diag(NULL,NULL,LARGE-1,0);CHECK(!config_reads && !stage_reads);
 prog_t program={0};program.serial=99;VkExec ex={0};ex.prog=&program;
 GfxShaderRes resource={.slot=2,.cbuf_slot=3,.handle=7,.flags=1};
 p_bind_buf bound={.addr=0x200000000,.size=0};descriptor.range=LARGE;
 VkExec original_ex=ex;GfxShaderRes original_resource=resource;p_bind_buf original_bound=bound;
 VkDescriptorBufferInfo original_descriptor=descriptor;
 gpu_write_binding_diag(&ex,5,&resource,&bound,&descriptor,0);
 if(on) {
  CHECK(descriptor_lines==1 && stage_reads==1 && origin_reads==1 && hash_reads==1);
  CHECK(strstr(last_log,"native_origin=000000abcdef1234") && strstr(last_log,"program=99 stage=5"));
  CHECK(strstr(last_log,"handle_word=7 handle_byte=28") && strstr(last_log,"declared=0 resolved=16777216"));
  CHECK(strstr(last_log,"extent_policy=backing-remainder-clipped-to-device"));
 }
 for(unsigned i=0;i<5;i++)gpu_write_binding_diag(&ex,5,&resource,&bound,&descriptor,0);
 CHECK(descriptor_lines==(on?1u:0u));
 CHECK(!memcmp(&ex,&original_ex,sizeof ex) && !memcmp(&resource,&original_resource,sizeof resource));
 CHECK(!memcmp(&bound,&original_bound,sizeof bound) && !memcmp(&descriptor,&original_descriptor,sizeof descriptor));
 bound.size=LARGE;gpu_write_binding_diag(&ex,5,&resource,&bound,&descriptor,0);
 if(on)CHECK(descriptor_lines==2 && strstr(last_log,"extent_policy=declared-clipped-to-backing-and-device"));
 gpu_write_binding_diag(&ex,5,&resource,&bound,&descriptor,1);CHECK(descriptor_lines==(on?3u:0u));
 for(unsigned i=0;i<160;i++){bound.addr=0x300000000ull+256*i;gpu_write_binding_diag(&ex,5,&resource,&bound,&descriptor,1);}
 CHECK(descriptor_lines==(on?128u:0u) && descriptor_limits==(on?1u:0u));
 unsigned reads=stage_reads;gpu_write_binding_diag(&ex,5,&resource,&bound,&descriptor,1);CHECK(stage_reads==reads);
 PoolRec pool={.storage_serial=7,.lo=0x2000,.size=UINT64_C(1)<<30,
  .import_lo=0x2000,.import_hi=(UINT64_C(1)<<30)+0x2000,.mstate=MS_ON,.mbuf=(VkBuffer)(uintptr_t)2};
 VkMemSpan span={.buffer=pool.mbuf,.offset=16,.avail=UINT64_C(1)<<30,.pool=&pool};
 PoolRec original_pool=pool;VkMemSpan original_span=span;
 mirror_write_diag(&pool,&span,LARGE,0x1100);
 if(on)CHECK(mirror_lines==1 && strstr(last_log,"caller_exe_rva=0x100") && strstr(last_log,"size=16777216"));
 for(unsigned i=0;i<5;i++)mirror_write_diag(&pool,&span,LARGE,0x1100);
 CHECK(mirror_lines==(on?1u:0u));
 CHECK(!memcmp(&pool,&original_pool,sizeof pool) && !memcmp(&span,&original_span,sizeof span));
 mirror_write_diag(&pool,&span,LARGE,0x1108);CHECK(mirror_lines==(on?2u:0u));
 for(unsigned i=0;i<80;i++){span.offset=256+256*i;mirror_write_diag(&pool,&span,LARGE,0x1100);}
 CHECK(mirror_lines==(on?64u:0u) && mirror_limits==(on?1u:0u));
 // Actual public marking behavior is independent of diagnostic enablement and
 // limits: retain all legal calls, byte counts and offsets exactly.
 span.pool=NULL;vk_mem_gpu_wrote(&span,LARGE);CHECK(!range_calls);
 span.pool=&pool;pool.mstate=0;vk_mem_gpu_wrote(&span,LARGE);CHECK(!range_calls);
 pool.mstate=MS_ON;span.buffer=(VkBuffer)(uintptr_t)3;vk_mem_gpu_wrote(&span,LARGE);CHECK(!range_calls);
 span.buffer=pool.mbuf;vk_mem_gpu_wrote(&span,0);CHECK(!range_calls);
 span.offset=17;vk_mem_gpu_wrote(&span,15);CHECK(range_calls==1 && last_range.r==&pool && last_range.off==17 && last_range.size==15);
 vk_mem_gpu_wrote(&span,LARGE);CHECK(range_calls==2 && last_range.off==17 && last_range.size==LARGE);
 CHECK(vertex_invalidations==5); // every nonempty write, including imported backing
 CHECK(config_reads==2);if(!on)CHECK(!stage_reads && !origin_reads && !hash_reads);
 puts("GPU write diagnostic checks passed");return 0;
}
'''


@unittest.skipUnless(os.name == "nt" and CC.is_file() and DEPS.is_dir(), "Windows Clang/Vulkan headers required")
class GpuWriteDiagnosticTests(unittest.TestCase):
    def test_passive_helpers_and_mirror_marking(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-gpu-write-diag-") as folder:
            path = Path(folder) / "fixture.c"
            path.write_text(source(), encoding="utf-8")
            for opt in ("-O0", "-O2"):
                executable = Path(folder) / "fixture.exe"
                compiled = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "runtime"),
                    "-I", str(DEPS), "-I", str(DEPS / "vma"), str(path), "-o", str(executable)], capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                for mode in ("1", "0", "unset", "2"):
                    with self.subTest(opt=opt, mode=mode):
                        run = subprocess.run([str(executable), mode], capture_output=True, text=True, timeout=10)
                        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
