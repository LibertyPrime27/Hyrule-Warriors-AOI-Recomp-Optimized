"""Original native tests for recorded and replayed non-indexed indirect draws."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
CC=Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
DEPS=Path("F:/TotKDependencies/runtime/deps/include")
sys.path.insert(0,str(ROOT/"tests"))
from nvn2_compute_c0_test import function

def source(mutation=None):
    draw=(ROOT/"runtime/vk_exec_draw.c").read_text()
    cmd=(ROOT/"runtime/nvn_api_cmd.c").read_text()
    code=(function(draw,"static int indirect_batch_on(")+function(draw,"static int indirect_can_stay_in_pass(")+
          function(draw,"static void op_draw(")+function(draw,"static void op_draw_ind("))
    if mutation=="no-quads":
        code=code.replace("if (d->prim == NVN_DRAW_PRIMITIVE_QUADS)","if (0)",1)
    if mutation=="zero-instance":
        code=code.replace("if (!count || !a.instanceCount) return;","if (!count) return;",1)
    if mutation=="lost-first":
        code=code.replace("(int32_t)a.firstVertex","0",1)
    before=r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include "vk_exec.h"
#define CHECK(x) do { if(!(x)) {fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);} } while(0)
#undef VK_MEM_USE
#undef VK_MEM_BOUND
#define VK_MEM_USE(s) do { used++; (s).buffer=(VkBuffer)(uintptr_t)0x2222; } while(0)
#define VK_MEM_BOUND(s) do { bound++; } while(0)
VkExecFrame vk_exec_frame;
static int gpu_quads_draw(VkExec *ex,const p_draw_ind *d){(void)ex;(void)d;return 0;}
static unsigned used,bound,barriers,reads,prepares,draws,indexed,notes,counts;
static unsigned expected_barriers=1;
static const char *batch_setting;
const char *cfg_get(const char *name){(void)name;return batch_setting;}
static int backed=1,read_ok=1,prepare_ok=1,unaligned,short_range,overflow;
static uint32_t c0_binst,indices[64],index_count,seen_instances,seen_base,seen_first,seen_count;
static VkDrawIndirectCommand gpu={7,2,11,3}, untouched={0,0,0,0};
void rt_fatal(const char *fmt,...) { (void)fmt;exit(73); }
void rt_log(const char *fmt,...) { (void)fmt; }
static int indirect_reset_draw(VkExec *ex,const p_draw *d) { (void)ex;(void)d;return 0; }
static int prepare_draw(VkExec *ex,int primitive,int32_t n,int restart) {
    (void)ex;CHECK(primitive==NVN_DRAW_PRIMITIVE_QUADS || primitive==NVN_DRAW_PRIMITIVE_TRIANGLES);
    CHECK(restart==0);seen_count=(uint32_t)n;prepares++;return prepare_ok;
}
static void bind_index(VkExec *ex,const void *data,uint64_t bytes,VkIndexType type) {
    CHECK(type==VK_INDEX_TYPE_UINT32 && bytes<=sizeof indices);memcpy(indices,data,(size_t)bytes);
    index_count=(unsigned)(bytes/4);ex->recorded++;
}
void rc_vkCmdDrawIndexed(VkCommandBuffer cb,uint32_t n,uint32_t inst,uint32_t first,int32_t vertex,uint32_t base) {
    (void)cb;CHECK(n==index_count && first==0 && vertex==0); indexed++;seen_instances=inst;seen_base=base;
}
void rc_vkCmdDraw(VkCommandBuffer cb,uint32_t n,uint32_t inst,uint32_t first,uint32_t base) {
    (void)cb;(void)n;(void)inst;(void)first;(void)base;CHECK(0 && "array indirect cannot become direct triangles");
}
void rc_vkCmdDrawIndirect(VkCommandBuffer cb,VkBuffer buffer,VkDeviceSize offset,uint32_t n,uint32_t stride) {
    (void)cb;CHECK(buffer==(VkBuffer)(uintptr_t)0x2222 && offset==32 && n==1 && stride==16);
    CHECK(used==1 && barriers==expected_barriers && prepares==1);draws++;
}
void vk_exec_barrier(VkExec *ex) { (void)ex;CHECK(used==1);barriers++; }
int vk_exec_read_buffer(VkExec *ex,uint64_t addr,uint64_t size,void *out) {
    (void)ex;CHECK(addr==0x10000 && size==16);reads++;if(read_ok)memcpy(out,&gpu,16);return read_ok;
}
int vk_mem_resolve(uint64_t addr,uint64_t size,VkMemSpan *out) {
    CHECK(addr==0x10000 && size==16);
    *out=(VkMemSpan){.buffer=(VkBuffer)(uintptr_t)0x1111,.offset=unaligned?2:overflow?UINT64_MAX-3:32,
        .avail=short_range?12:16};return backed;
}
void vk_mem_count(int cls,int use,uint64_t bytes) { (void)cls;CHECK(use==VK_MUSE_IND && bytes==16);counts++; }
static void note_work(const VkExec *ex,int kind,uint64_t work) {
    (void)ex;(void)work;CHECK(kind==0 || kind==1);notes++;
}
static p_draw_ind recorded;
static int record_op;
static void cmd_rt_trace(Cpu *c,const char *s) { (void)c;CHECK(!strcmp(s,"DrawArraysIndirect")); }
static void *cmd_of(Cpu *c) { CHECK(c->x[0]==123);return &recorded; }
static void *fixture_rec(void *cmd,int op,size_t size) {
    CHECK(cmd==&recorded && size==sizeof recorded);record_op=op;return &recorded;
}
#undef NVN_API
#define NVN_API(name) static void fixture_##name(Cpu *c)
#undef REC
#define REC(cmd,op,type) ((type*)fixture_rec(cmd,op,sizeof(type)))
'''
    api=function(cmd,"NVN_API(nvnCommandBufferDrawArraysIndirect)")
    after=r'''
int main(int argc,char **argv) {
    CHECK(argc==2);const char *mode=argv[1];
    Cpu cpu={0};cpu.x[0]=123;cpu.x[1]=7;cpu.x[2]=0x10000;cpu.x[3]=0xdeadbeef;
    fixture_nvnCommandBufferDrawArraysIndirect(&cpu);
    CHECK(record_op==OP_DRAW_IND && recorded.prim==7 && recorded.pad==0 && recorded.ind==0x10000);
    VkExec ex={0};p_draw_ind d={NVN_DRAW_PRIMITIVE_QUADS,0,0x10000};
    if(!strcmp(mode,"native")) d.prim=NVN_DRAW_PRIMITIVE_TRIANGLES;
    else if(!strcmp(mode,"native-batched")) {d.prim=NVN_DRAW_PRIMITIVE_TRIANGLES;batch_setting="1";ex.in_pass=1;expected_barriers=0;}
    else if(!strcmp(mode,"native-writer")) {d.prim=NVN_DRAW_PRIMITIVE_TRIANGLES;batch_setting="1";ex.in_pass=1;ex.pass_storage_writes=1;}
    else if(!strcmp(mode,"zero")) gpu.vertexCount=0;
    else if(!strcmp(mode,"partial")) gpu.vertexCount=3;
    else if(!strcmp(mode,"zero-instance")) gpu.instanceCount=0;
    else if(!strcmp(mode,"disabled")) ex.render_disabled=1;
    else if(!strcmp(mode,"bad-primitive"))d.prim=NVN_DRAW_PRIMITIVE_POLYGON;
    else if(!strcmp(mode,"bad-address"))d.ind++;
    else if(!strcmp(mode,"overflow-address"))d.ind=UINT64_MAX-3;
    else if(!strcmp(mode,"unbacked")){d.prim=NVN_DRAW_PRIMITIVE_TRIANGLES;backed=0;}
    else if(!strcmp(mode,"unaligned")){d.prim=NVN_DRAW_PRIMITIVE_TRIANGLES;unaligned=1;}
    else if(!strcmp(mode,"short")){d.prim=NVN_DRAW_PRIMITIVE_TRIANGLES;short_range=1;}
    else if(!strcmp(mode,"overflow-offset")){d.prim=NVN_DRAW_PRIMITIVE_TRIANGLES;overflow=1;}
    else if(!strcmp(mode,"read-fail"))read_ok=0;
    else if(!strcmp(mode,"too-many"))gpu.vertexCount=(1u<<20)+4;
    else if(!strcmp(mode,"first-overflow"))gpu.firstVertex=UINT32_MAX;
    else if(!strcmp(mode,"instance-overflow"))gpu.instanceCount=UINT32_MAX;
    else if(!strcmp(mode,"base-overflow"))gpu.firstInstance=UINT32_MAX;
    else if(!strcmp(mode,"prepare-fail"))prepare_ok=0;
    else CHECK(!strcmp(mode,"quads"));
    op_draw_ind(&ex,&d);
    CHECK(!memcmp(&untouched,(uint32_t[4]){0,0,0,0},16));
    if(d.prim==NVN_DRAW_PRIMITIVE_TRIANGLES) {
        CHECK(!reads && draws==1 && !indexed && ex.recorded==1 && vk_exec_frame.draws==1);
        CHECK(bound==1 && counts==1 && c0_binst==0);
    } else if(ex.render_disabled) CHECK(!reads && !prepares && !indexed);
    else if(gpu.vertexCount<4 || !gpu.instanceCount)CHECK(reads==1 && !prepares && !indexed);
    else if(!prepare_ok)CHECK(reads==1 && prepares==1 && !indexed && !draws);
    else {
        const uint32_t expected[6]={11,12,13,11,13,14};
        CHECK(reads==1 && prepares==1 && indexed==1 && !draws && index_count==6);
        CHECK(!memcmp(indices,expected,sizeof expected) && seen_count==4);
        CHECK(seen_instances==2 && seen_base==3 && c0_binst==3);
        CHECK(ex.recorded==2 && vk_exec_frame.draws==1);
    }
    puts("array indirect native checks passed");return 0;
}
'''
    return before+api+code+after

@unittest.skipUnless(os.name=="nt" and CC.is_file() and DEPS.is_dir(),"Windows Clang/Vulkan headers required")
class DrawArraysIndirectTests(unittest.TestCase):
    def test_production_recording_replay_and_failure_paths(self):
        out=ROOT/"work/build";out.mkdir(parents=True,exist_ok=True)
        good=("quads","native","native-batched","native-writer","zero","partial","zero-instance","disabled","prepare-fail")
        bad=("bad-primitive","bad-address","overflow-address","unbacked","unaligned","short","overflow-offset",
             "read-fail","too-many","first-overflow","instance-overflow","base-overflow")
        with tempfile.TemporaryDirectory(prefix="arrays-indirect-",dir=out) as folder:
            p=Path(folder)/"fixture.c";exe=p.with_suffix(".exe")
            for opt in ("-O0","-O2"):
                p.write_text(source(),encoding="utf-8")
                r=subprocess.run([str(CC),opt,"-I",str(ROOT/"runtime"),"-I",str(DEPS),"-I",str(DEPS/"vma"),
                    str(p),"-o",str(exe)],capture_output=True,text=True,timeout=60)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                for mode in good+bad:
                    r=subprocess.run([str(exe),mode],capture_output=True,text=True,timeout=10)
                    self.assertEqual(r.returncode,0 if mode in good else 73,mode+r.stdout+r.stderr)
                for mutation,mode in (("no-quads","quads"),("zero-instance","zero-instance"),("lost-first","quads")):
                    p.write_text(source(mutation),encoding="utf-8")
                    subprocess.run([str(CC),opt,"-I",str(ROOT/"runtime"),"-I",str(DEPS),"-I",str(DEPS/"vma"),
                        str(p),"-o",str(exe)],check=True,capture_output=True,timeout=60)
                    r=subprocess.run([str(exe),mode],capture_output=True,timeout=10)
                    self.assertNotEqual(r.returncode,0,mutation)
@unittest.skipUnless(os.name=="nt" and os.environ.get("HWAOI_INDIRECT_TEST_EXE"),
                     "set HWAOI_INDIRECT_TEST_EXE for the original Vulkan fixture")
class DrawArraysIndirectGpuTests(unittest.TestCase):
    def test_gpu_written_arguments_and_quad_pixels(self):
        sys.path.insert(0,str(ROOT/"tools"))
        from hwaoi_probe import capture
        report=ROOT/"work/reports"/("arrays-indirect-gpu-"+uuid4().hex[:12])
        with patch.dict(os.environ,{"HWAOI_PROBE_ARRAYS_INDIRECT":"1","HWAOI_VK_VALIDATION":"1",
                                   "HWAOI_GPU":"vulkan","HWAOI_FIX_C0":"0","HWAOI_PIPE_SYNC":"1",
                                   "HWAOI_EXPERIMENTAL_GEOMETRY":"1"}):
            result=capture(Path(os.environ["HWAOI_INDIRECT_TEST_EXE"]),"--probe-vkgeometry",report,60)
        self.assertFalse(result["timed_out"],result)
        self.assertEqual(result["exit_code"],0,result)
        output=Path(result["stdout"]).read_text(encoding="utf-8",errors="replace")
        errors=Path(result["stderr"]).read_text(encoding="utf-8",errors="replace")
        self.assertIn("arrays-indirect passed (8 cases, GPU-produced parameters, QUADS, zero counts/instances, 8192 exact pixels)",output)
        self.assertIn("0 validation errors",output)
        self.assertIn("validation layer and error reporting enabled",errors)
        self.assertNotIn("vk validation error:",errors)

if __name__=="__main__": unittest.main()
