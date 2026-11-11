"""Real mirror interval/copy code vs independent page coverage and GPU-gap canaries."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
CC=Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
DEPS=Path("F:/TotKDependencies/runtime/deps/include")
sys.path.insert(0,str(ROOT/"tests"))
from nvn2_compute_c0_test import function


def source(mutation=False):
    text=(ROOT/"runtime/vk_memory.c").read_text()
    functions="\n".join(function(text,s) for s in ("static void range_add(","static int range_cmp(",
        "static void mirror_flush_add(","static void mirror_flush_drain(","static int copy_ranges("))
    if mutation:
        functions=functions.replace("r->flush[first] = (MirrorFlush){lo, hi};",r'''
        r->flush[first] = (MirrorFlush){lo, hi};
        if (r->nflush>1) {r->flush[0].hi=r->flush[r->nflush-1].hi;r->nflush=1;}
''')
    return r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vulkan/vulkan.h>
#define CHECK(x) do{if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
typedef struct {uint64_t lo,hi;} MirrorFlush;
typedef struct {uint64_t import_lo,import_hi;MirrorFlush *flush;int nflush,cflush;VkBuffer buf,mbuf;} PoolRec;
typedef struct {PoolRec *r;uint64_t off,size;} MRange;
static MRange *pend;static int npend,cpend;
static VkBufferCopy *mregions;static int cregions;
static uint8_t wanted[64],copied[64],cpu[64],gpu[64];static int small=1;
static uint64_t recorded_bytes;
static void grow(void **p,int *cap,size_t size) {
    *cap=*cap ? *cap*2 : 8;*p=realloc(*p,(size_t)*cap*size);CHECK(*p);
}
VKAPI_ATTR void VKAPI_CALL vkCmdCopyBuffer(VkCommandBuffer cb,VkBuffer from,VkBuffer to,uint32_t n,const VkBufferCopy *rs) {
    (void)cb;CHECK(from==(VkBuffer)(uintptr_t)1 && to==(VkBuffer)(uintptr_t)2);
    for(uint32_t i=0;i<n;i++) {
        CHECK(rs[i].srcOffset==rs[i].dstOffset && rs[i].size && !(rs[i].size%4096));
        recorded_bytes+=rs[i].size;
        if(!small)continue;
        CHECK(rs[i].srcOffset/4096+rs[i].size/4096<=64);
        for(uint64_t k=rs[i].srcOffset/4096;k<(rs[i].srcOffset+rs[i].size)/4096;k++) {
            CHECK(wanted[k]);copied[k]=1;gpu[k]=cpu[k];
        }
    }
}
''' + functions + r'''
static uint32_t rng=0x3a508e76;
static uint32_t rnd(void){rng^=rng<<13;rng^=rng>>17;rng^=rng<<5;return rng;}
static void add(PoolRec *r,uint64_t off,uint64_t size) {
    mirror_flush_add(r,r->import_lo+off,r->import_lo+off+size);
    if(small)for(unsigned k=0;k<64;k++)if((uint64_t)k*4096<off+size && (uint64_t)(k+1)*4096>off)wanted[k]=1;
}
static void check_drain(PoolRec *r) {
    mirror_flush_drain(r);CHECK(r->nflush==0);
    uint64_t bytes=0,copies=0;copy_ranges(VK_NULL_HANDLE,pend,npend,1,&bytes,&copies);
    CHECK(bytes==recorded_bytes);npend=0;recorded_bytes=0;
    if(small)for(unsigned k=0;k<64;k++) {
        CHECK(copied[k]==wanted[k]);CHECK(gpu[k]==(wanted[k] ? cpu[k] : (uint8_t)0xa5));
    }
}
int main(void) {
    PoolRec r={.import_lo=0x100000,.import_hi=0x140000,.buf=(VkBuffer)(uintptr_t)1,.mbuf=(VkBuffer)(uintptr_t)2};
    memset(cpu,0x32,sizeof cpu);memset(gpu,0xa5,sizeof gpu);
    add(&r,4096+7,20);add(&r,60*4096+3,100);check_drain(&r);
    CHECK(!copied[32] && gpu[32]==0xa5); // untouched GPU-owned gap
    for(unsigned round=0;round<128;round++) {
        memset(wanted,0,sizeof wanted);memset(copied,0,sizeof copied);memset(gpu,0xa5,sizeof gpu);
        for(unsigned j=0;j<128;j++) {
            uint64_t off=rnd()%(64*4096),len=1+rnd()%2048;if(len>64*4096-off)len=64*4096-off;
            add(&r,off,len);
            for(int i=1;i<r.nflush;i++)CHECK(r.flush[i-1].hi<r.flush[i].lo);
        }
        check_drain(&r);
    }
    // Realistic video-like distant planes: exactly two pages, not the 3 GiB envelope.
    small=0;r.import_hi=r.import_lo+(UINT64_C(4)<<30);
    add(&r,17,200);add(&r,(UINT64_C(3)<<30)+31,500);
    mirror_flush_drain(&r);CHECK(npend==2 && pend[0].size+pend[1].size==8192);
    uint64_t bytes=0,copies=0;copy_ranges(VK_NULL_HANDLE,pend,npend,1,&bytes,&copies);CHECK(bytes==8192 && copies==2);
    free(r.flush);free(pend);free(mregions);puts("mirror flush range checks passed");return 0;
}
'''


@unittest.skipUnless(os.name=="nt" and CC.is_file() and DEPS.is_dir(),"Windows Clang/Vulkan headers required")
class MirrorFlushRangeTests(unittest.TestCase):
    def test_exact_coverage_and_old_envelope_negative_control(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-mirror-flush-") as temp:
            for opt in ("-O0","-O2"):
                for mutation in (False,True):
                    with self.subTest(opt=opt,mutation=mutation):
                        p=Path(temp)/"fixture.c";exe=Path(temp)/"fixture.exe"
                        p.write_text(source(mutation),encoding="utf-8")
                        build=subprocess.run([str(CC),opt,"-Wall","-Wextra","-Werror","-I",str(DEPS),
                            str(p),"-o",str(exe)],capture_output=True,text=True)
                        self.assertEqual(build.returncode,0,build.stderr)
                        run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=10)
                        self.assertEqual(run.returncode==0,not mutation,run.stdout+run.stderr)


if __name__=="__main__":unittest.main()
