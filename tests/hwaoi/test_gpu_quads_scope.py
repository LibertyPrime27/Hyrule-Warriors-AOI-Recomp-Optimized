"""NVN2-only GPU QUADS admission, batched conversion and restoration of guest graphics binding state."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function
CC = Path("F:/llvm-mingw/bin/clang.exe")
# Vulkan/VMA headers: HWAOI_TEST_DEPS (a deps tree with include/), e.g. a compiled game's work/toolchain/deps.
DEPS = Path(os.environ.get("HWAOI_TEST_DEPS") or "F:/TotKDependencies/runtime/deps") / "include"


def between(text, start, end):
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


def source():
    text = (ROOT / "runtime/vk_exec_draw.c").read_text()
    prefix = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "game_target.h"
#include "nvn_api.h"
#include "vk_quad.h"
#define CHECK(x) do{if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
#define NVN_STAGE_VS 0
#define NVN_STAGE_FS 1
#define NVN_STAGE_GS 2
#define NVN_STAGE_TCS 3
// program_stage_lookup takes NVN stage numbers (0 VS, 1 FS, 2 GS, 3 TCS, 4 TES): the stubs use them too.
static unsigned present=(1u<<NVN_STAGE_VS)|(1u<<NVN_STAGE_FS),nvn2=~0u;
static unsigned reset,prepares,indices,draws,notes,counts,batches,batched;
static int backed=1,converted=1,draw_ok=1;
static const char *setting="1";
static int ab;
void rt_log(const char *fmt,...){(void)fmt;}
int rt_ab(void){return ab;}
static uint64_t bind_gen=9;static uint32_t c0_binst=99;
VkExecFrame vk_exec_frame;int vk_mirror_on;uint32_t rt_trace;
static const prog_t *program_stage_source(const VkExec *ex,int stage){(void)stage;return ex->prog;}
static int program_stage_lookup(const prog_t *p,unsigned stage,NVNshaderData *out){
 (void)p;if(!(present&(1u<<stage)))return 0;out->control=(const uint8_t *)(uintptr_t)(stage+1);out->data=1;return 1;}
int gfx_shader_is_nvn2(const uint8_t *control){return !!(nvn2&(1u<<((uintptr_t)control-1)));}
const char *cfg_get(const char *key){CHECK(!strcmp(key,"TOTK_GPU_QUADS"));return setting;}
int vk_mem_resolve(uint64_t addr,uint64_t size,VkMemSpan *out){CHECK((addr&~0xf0ull)==0x10000 && size==16);
 *out=(VkMemSpan){.buffer=(VkBuffer)(uintptr_t)22,.offset=addr&0xf0,.avail=16};return backed;}
void vk_mem_use(VkMemSpan *span){(void)span;CHECK(0);}
void vk_mem_bound_note(void *pool){(void)pool;CHECK(0);}
void vk_mem_count(int cls,int use,uint64_t bytes){(void)cls;CHECK(use==VK_MUSE_IND && bytes==16);counts++;}
int vk_quad_indirect_prepare_batch(VkExec *ex,const VkMemSpan *sources,const uint64_t *addresses,uint32_t n,
                                   VkQuadIndirect *out,uint8_t *ok){
 CHECK(n>=1 && n<=64);batches++;batched+=n;if(!converted)return 0;
 for(uint32_t i=0;i<n;i++){CHECK(sources[i].buffer==(VkBuffer)(uintptr_t)22 && sources[i].offset==(addresses[i]&0xf0));
  out[i]=(VkQuadIndirect){(VkBuffer)(uintptr_t)33,(VkBuffer)(uintptr_t)44,512+64*(VkDeviceSize)i};ok[i]=1;}
 ex->recorded+=8;return (int)n;
}
void vk_exec_draw_new_cb(void){reset++;}
int vk_exec_barrier_needed(int bits){return (bits&0xf)||(bits&~0xff);}   // vk_exec.c's rule: ORDER bits record a barrier
static int prepare_draw(VkExec *ex,int primitive,int32_t count,int restart){(void)ex;
 CHECK(primitive==NVN_DRAW_PRIMITIVE_QUADS && count==INT32_MAX && restart==0);prepares++;return draw_ok;}
static VkDeviceSize expected_offset=512;
void rc_vkCmdBindIndexBuffer(VkCommandBuffer cb,VkBuffer b,VkDeviceSize offset,VkIndexType type){
 (void)cb;CHECK(b==(VkBuffer)(uintptr_t)33 && !offset && type==VK_INDEX_TYPE_UINT32);indices++;}
void rc_vkCmdDrawIndexedIndirect(VkCommandBuffer cb,VkBuffer b,VkDeviceSize offset,uint32_t count,uint32_t stride){
 (void)cb;CHECK(b==(VkBuffer)(uintptr_t)44 && offset==expected_offset && count==1 && stride==20);draws++;}
static void note_work(const VkExec *ex,int kind,uint64_t work){(void)ex;CHECK(kind==1 && !work);notes++;}
// A command list: QUADS draws at argument records 0x10000 + 16k, with the given op between consecutive draws.
static uint64_t list[64];
static int barrier_bits;
static const p_draw_ind *record(unsigned draws_wanted,uint32_t between){
 uint8_t *at=(uint8_t *)list;const p_draw_ind *first=NULL;
 for(unsigned k=0;k<draws_wanted;k++){
  if(k && between){op_hdr *s=(op_hdr *)at;s->op=between;
   s->size=sizeof(op_hdr)+(between==OP_COPY_B2B?sizeof(p_copy_b2b):between==OP_BARRIER?sizeof(p_barrier):sizeof(p_scissor));
   memset(s+1,0,s->size-sizeof *s);if(between==OP_BARRIER)((p_barrier *)(s+1))->bits=barrier_bits;at+=s->size;}
  op_hdr *h=(op_hdr *)at;h->op=OP_DRAW_IND;h->size=sizeof(op_hdr)+sizeof(p_draw_ind);
  p_draw_ind *d=(p_draw_ind *)(h+1);*d=(p_draw_ind){NVN_DRAW_PRIMITIVE_QUADS,0,0x10000+16*(uint64_t)k};if(!k)first=d;
  at+=h->size;
 }
 op_hdr *end=(op_hdr *)at;end->op=OP_END;end->size=sizeof(op_hdr);return first;
}
static const p_draw_ind *next_draw(const p_draw_ind *d){
 const op_hdr *o=nvn_op_next((const op_hdr *)(const void *)d-1);while(o->op!=OP_DRAW_IND)o=nvn_op_next(o);return nvn_op_payload(o);}
'''
    batching = between(text, "// Why gpu_quads_draw declined a draw", "static int gpu_quads_graphics(")
    batching += between(text, "// ---- batched QUADS conversion", "static int gpu_quads_draw(")
    main = r'''
int main(int argc,char **argv){
 CHECK(argc==2);VkExec ex={.prog=(const prog_t *)(uintptr_t)1,.fence=1};int why,stage;
 CHECK(gpu_quads_graphics(&ex,&why,&stage));
 // A fragment stage is the NVN stage 1: the former GFX_STAGE_* loop read it as TCS and declined every program.
 nvn2&=~(1u<<NVN_STAGE_FS);CHECK(!gpu_quads_graphics(&ex,&why,&stage) && why==QF_NOT_NVN2 && stage==NVN_STAGE_FS);nvn2=~0u;
 present|=1u<<NVN_STAGE_GS;CHECK(!gpu_quads_graphics(&ex,&why,&stage) && why==QF_STAGE && stage==NVN_STAGE_GS);present&=~(1u<<NVN_STAGE_GS);
 present|=1u<<NVN_STAGE_TCS;CHECK(!gpu_quads_graphics(&ex,&why,&stage) && why==QF_STAGE);present&=~(1u<<NVN_STAGE_TCS);
 present=1u<<NVN_STAGE_FS;CHECK(!gpu_quads_graphics(&ex,&why,&stage) && why==QF_NO_VERTEX);present=(1u<<NVN_STAGE_VS)|(1u<<NVN_STAGE_FS);
 if(!strcmp(argv[1],"batch")||!strcmp(argv[1],"nobatch")||!strcmp(argv[1],"invalidate")||!strcmp(argv[1],"order")){
  // Scissor and texture-invalidation-only barriers (0x10, recorded as nothing) separate batched draws; a copy or an
  // ORDER_FRAGMENTS barrier (0x2) ends the batch.
  int batch=!strcmp(argv[1],"batch")||!strcmp(argv[1],"invalidate");
  uint32_t between=!strcmp(argv[1],"batch")?OP_SCISSOR:!strcmp(argv[1],"nobatch")?OP_COPY_B2B:OP_BARRIER;
  barrier_bits=!strcmp(argv[1],"invalidate")?0x10:0x2;
  const p_draw_ind *a=record(3,between),*b=next_draw(a),*c=next_draw(b);
  CHECK(gpu_quads_draw(&ex,a));
  CHECK(batches==1 && batched==(batch?3u:1u) && reset==1 && counts==(batch?3u:1u));
  expected_offset=batch?512+64:512;CHECK(gpu_quads_draw(&ex,b));
  CHECK(batches==(batch?1u:2u) && reset==(batch?1u:2u));   // a batched draw records no conversion and keeps its bindings
  expected_offset=batch?512+128:512;CHECK(gpu_quads_draw(&ex,c));
  CHECK(batches==(batch?1u:3u) && draws==3 && vk_exec_frame.quads_gpu==3);
  ex.fence=2;expected_offset=512;CHECK(gpu_quads_draw(&ex,b) && batches==(batch?2u:4u));   // another submission converts anew
  puts("GPU QUADS batching checks passed");return 0;
 }
 const p_draw_ind *draw=record(1,0);
 if(!strcmp(argv[1],"off"))setting="0";
 else if(!strcmp(argv[1],"legacy"))nvn2=0;
 else if(!strcmp(argv[1],"unbacked"))backed=0;
 else if(!strcmp(argv[1],"allocation"))converted=0;
 else if(!strcmp(argv[1],"skip"))draw_ok=0;
 else if(!strcmp(argv[1],"ab-off"))setting="ab";
 else if(!strcmp(argv[1],"ab-on")){setting="ab";ab=1;}
 else CHECK(!strcmp(argv[1],"good"));
 int result=gpu_quads_draw(&ex,draw),accepted=!strcmp(argv[1],"good")||!strcmp(argv[1],"skip")||!strcmp(argv[1],"ab-on");
 CHECK(result==accepted);
 if(!accepted){CHECK(!reset && !prepares && !draws && bind_gen==9 && c0_binst==99 && !vk_exec_frame.quads_gpu);}
 else{CHECK(reset==1 && prepares==1 && bind_gen==10 && c0_binst==0 && vk_exec_frame.quads_gpu==1);
  CHECK(counts==1 && (ex.dirty&VK_EXEC_DIRTY_ALL)==VK_EXEC_DIRTY_ALL);
  CHECK(draws==(unsigned)draw_ok && indices==(unsigned)draw_ok && notes==(unsigned)draw_ok);
  CHECK(ex.recorded==(draw_ok?10:8));}
 puts("GPU QUADS scope checks passed");return 0;
}
'''
    return (prefix + batching + function(text, "static int gpu_quads_graphics(") + function(text, "static int gpu_quads_draw(")
            + main)


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Windows Clang required")
class GpuQuadScopeTests(unittest.TestCase):
    def test_abi_fallback_and_cache_restore(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-quad-scope-") as tmp:
            p = Path(tmp) / "scope.c"; p.write_text(source(), encoding="utf-8"); exe = p.with_suffix(".exe")
            for opt in ("-O0", "-O2"):
                result = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers",
                                         "-I", str(ROOT / "runtime"), "-I", str(DEPS), "-I", str(DEPS / "vma"),
                                         str(p), "-o", str(exe)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for case in ("good", "off", "legacy", "unbacked", "allocation", "skip", "ab-off", "ab-on", "batch", "nobatch",
                             "invalidate", "order"):
                    result = subprocess.run([str(exe), case], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
                    self.assertEqual(result.returncode, 0, case + ": " + result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
