"""Original copy-pitch fixtures run the actual transfer layout calculation."""
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


def source():
    xfer=(ROOT/"runtime/vk_exec_xfer.c").read_text()
    return r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "vk_exec.h"
#define CHECK(x) do {if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
#undef VK_MEM_PICK
#define VK_MEM_PICK(m) do {(void)(m);}while(0)
enum {W_REGION,W_BUFFER};
typedef struct {uint32_t w,h,nz;int bw,bh,bpb;} Sub;
static int backed=1;static uint64_t available=UINT64_MAX,asked;
void rt_fatal(const char *s,...) {(void)s;exit(73);}
static void warn_once(int n,const char *s,...) {(void)n;(void)s;}
int vk_mem_resolve(uint64_t addr,uint64_t size,VkMemSpan *out) {
    CHECK(addr==0x1000);asked=size;memset(out,0,sizeof *out);out->avail=available;out->offset=256;return backed;
}
static VkImageSubresourceLayers sub_layers(const Sub *s) {
    return (VkImageSubresourceLayers){VK_IMAGE_ASPECT_COLOR_BIT,0,0,s->nz};
}
static VkOffset3D sub_offset(const Sub *s) {(void)s;return (VkOffset3D){0,0,0};}
static VkExtent3D sub_extent(const Sub *s) {return (VkExtent3D){s->w,s->h,1};}
''' + function(xfer,"static int buffer_side(") + r'''
int main(int argc,char **argv) {
    CHECK(argc==2);VkExec ex={0};Sub s={5,3,2,1,1,4};
    uint64_t size;VkBufferImageCopy copy;VkMemSpan span;int expect=1;
    if(!strcmp(argv[1],"padded")){ex.copy_stride=32;ex.copy_image_stride=160;}
    else if(!strcmp(argv[1],"blocks")){s=(Sub){9,5,3,4,4,16};ex.copy_stride=64;ex.copy_image_stride=256;}
    else if(!strcmp(argv[1],"short-row")){ex.copy_stride=16;expect=0;}
    else if(!strcmp(argv[1],"negative-row"))ex.copy_stride=-1;
    else if(!strcmp(argv[1],"negative-image"))ex.copy_image_stride=-1;
    else if(!strcmp(argv[1],"short-image"))ex.copy_image_stride=40;
    else if(!strcmp(argv[1],"partial-row"))ex.copy_image_stride=61;
    else if(!strcmp(argv[1],"row-overflow"))ex.copy_stride=INT64_MAX-3;
    else if(!strcmp(argv[1],"slice-overflow")){s.nz=UINT32_MAX;ex.copy_stride=4000000000ll;ex.copy_image_stride=INT64_C(8000000000000000000);}
    else if(!strcmp(argv[1],"unbacked")){backed=0;expect=0;}
    else if(!strcmp(argv[1],"short-span")){available=10;expect=0;}
    else CHECK(!strcmp(argv[1],"tight"));
    int ok=buffer_side(&ex,&s,0x1000,&size,&copy,&span);CHECK(ok==expect);if(!ok)return 0;
    uint64_t row=ex.copy_stride ? ex.copy_stride : ((uint64_t)s.w+s.bw-1)/s.bw*s.bpb;
    uint64_t rows=((uint64_t)s.h+s.bh-1)/s.bh;
    uint64_t pitch=ex.copy_image_stride ? ex.copy_image_stride : row*rows;
    uint64_t last=pitch*(s.nz-1)+row*(rows-1)+((uint64_t)s.w+s.bw-1)/s.bw*s.bpb;
    CHECK(size==last && asked==last && copy.bufferOffset==256);
    CHECK(copy.bufferRowLength==row/s.bpb*s.bw && copy.bufferImageHeight==pitch/row*s.bh);
    CHECK(copy.imageSubresource.layerCount==s.nz);return 0;
}
'''


@unittest.skipUnless(os.name=="nt" and CC.is_file() and DEPS.is_dir(),"Windows Clang/Vulkan headers required")
class CopyImageStrideTests(unittest.TestCase):
    def test_pitch_regions_and_rejected_layouts(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-image-pitch-") as temp:
            path=Path(temp)/"fixture.c";path.write_text(source(),encoding="utf-8")
            for opt in ("-O0","-O2"):
                exe=Path(temp)/"fixture.exe"
                build=subprocess.run([str(CC),opt,"-I",str(ROOT/"runtime"),"-I",str(DEPS),
                    "-I",str(DEPS/"vma"),str(path),"-o",str(exe)],capture_output=True,text=True)
                self.assertEqual(build.returncode,0,build.stderr)
                for case in ("tight","padded","blocks","short-row","negative-row","negative-image",
                             "short-image","partial-row","row-overflow","slice-overflow","unbacked","short-span"):
                    with self.subTest(opt=opt,case=case):
                        run=subprocess.run([str(exe),case],capture_output=True,text=True,timeout=5)
                        bad=case in ("negative-row","negative-image","short-image","partial-row","row-overflow","slice-overflow")
                        self.assertEqual(run.returncode,73 if bad else 0,run.stdout+run.stderr)


if __name__=="__main__":unittest.main()
