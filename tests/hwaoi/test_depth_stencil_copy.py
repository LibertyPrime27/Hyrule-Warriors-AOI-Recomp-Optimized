"""Synthetic depth/stencil transfer metadata and copied production helper tests."""
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
    xfer = (ROOT / "runtime/vk_exec_xfer.c").read_text()
    return r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include "vk_exec.h"
#define CHECK(x) do { if(!(x)) { fprintf(stderr,"check line %d: %s\n",__LINE__,#x);exit(1); } } while(0)
static unsigned logs;
static char line[4096];
void rt_log(const char *fmt,...) {
    va_list ap;va_start(ap,fmt);vsnprintf(line,sizeof line,fmt,ap);va_end(ap);logs++;
}
''' + function(xfer, "static int combined_depth_stencil(") + function(xfer, "static void depth_copy_diag(") + r'''
int main(int argc,char **argv) {
    CHECK(argc==2);
    _putenv_s("HWAOI_IMAGE_DIAG",argv[1]);
    VkExec ex={.copy_stride=8192};
    tex_t tex={.serial=23,.mem=(uint8_t *)(uintptr_t)0x12340000,
        .d={.format=NVN_FORMAT_DEPTH32F_STENCIL8,.target=NVN_TEXTURE_TARGET_2D_ARRAY,
            .width=512,.height=256,.depth=1,.levels=4,.samples=1,.flags=9},
        .L={.layers=12,.size=123456}};
    view_t view={.set_levels=1,.base_level=2,.nlevels=1,.set_layers=1,.min_layer=6,.nlayers=3,
        .set_dsmode=1,.dsmode=1,.set_swizzle=1,.swz={0,1,2,3}};
    NVNcopyRegion region={3,4,1,64,32,2};
    const tex_t before=tex;const view_t old_view=view;const NVNcopyRegion old_region=region;
    CHECK(combined_depth_stencil(&tex));
    CHECK(!combined_depth_stencil(NULL));
    tex.d.format=NVN_FORMAT_DEPTH24_STENCIL8;CHECK(combined_depth_stencil(&tex));
    tex.d.format=NVN_FORMAT_DEPTH32F;CHECK(!combined_depth_stencil(&tex));tex=before;
    for(unsigned i=0;i<100;i++)depth_copy_diag(&ex,"T2T","src",&tex,&view,1,&region,4,0x67890000);
    CHECK(!memcmp(&tex,&before,sizeof tex) && !memcmp(&view,&old_view,sizeof view) &&
          !memcmp(&region,&old_region,sizeof region) && !ex.recorded);
    if(!strcmp(argv[1],"1")) {
        CHECK(logs==64);
        CHECK(strstr(line,"depth-xfer n=64 op=T2T side=src flags=0x4 stride=8192 buffer=0x67890000"));
        CHECK(strstr(line,"serial=23 addr=0x12340000 size=123456 format=0x36"));
        CHECK(strstr(line,"shape=512x256x1 levels=4 layers=12 samples=1"));
        CHECK(strstr(line,"mip=2/1 layer=6/3 dsmode=1 swizzle=0/1/2/3 region=3,4,1/64,32,2"));
    } else CHECK(!logs);
    puts("depth transfer diagnostic passed");return 0;
}
'''


def copy_source():
    xfer = (ROOT / "runtime/vk_exec_xfer.c").read_text()
    start = xfer.index("typedef struct {\n    const VkTexImage *img;")
    sub = xfer[start:xfer.index("// Resolves tex,", start)]
    preamble = r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include "tex.h"
#include "vk_exec.h"
#define CHECK(x) do { if(!(x)) { fprintf(stderr,"check line %d: %s\n",__LINE__,#x);exit(1); } } while(0)
static tex_t source_tex, destination_tex;
static VkTexImage images[2];
static unsigned lookups, pass_ends, barriers, copies, written;
static int missing;
static VkImageCopy received;
static VkExec *execution;
void rt_fatal(const char *fmt,...) {
    CHECK(!pass_ends && !barriers && !copies && !written && !execution->recorded);
    va_list ap;va_start(ap,fmt);vfprintf(stderr,fmt,ap);va_end(ap);exit(73);
}
const VkTexImage *vk_texture_get(const tex_t *t) {
    CHECK(t==&source_tex || t==&destination_tex);lookups++;
    if(missing && t==&source_tex)return NULL;
    return &images[t==&destination_tex];
}
void vk_exec_pass_end(VkExec *ex) {CHECK(ex==execution && !pass_ends && !barriers && !copies);pass_ends++;}
void vk_exec_barrier(VkExec *ex) {CHECK(ex==execution && pass_ends==1 && !barriers && !copies);barriers++;}
VKAPI_ATTR void VKAPI_CALL vkCmdCopyImage(VkCommandBuffer cb,VkImage src,VkImageLayout sl,
    VkImage dst,VkImageLayout dl,uint32_t count,const VkImageCopy *regions) {
    CHECK(cb==execution->cb && pass_ends==1 && barriers==1 && !copies);
    CHECK(src==images[0].image && dst==images[1].image && count==1);
    CHECK(sl==VK_IMAGE_LAYOUT_GENERAL && dl==VK_IMAGE_LAYOUT_GENERAL);
    received=regions[0];copies++;
}
void vk_texture_gpu_written(const tex_t *t) {CHECK(t==&destination_tex && copies==1 && execution->recorded==1);written++;}
'''
    funcs = ["static uint32_t max1(", "static int combined_depth_stencil(",
             "static VkImageSubresourceLayers sub_layers(", "static VkOffset3D sub_offset(",
             "static VkExtent3D sub_extent(", "static int depth_copy_sub(",
             "static _Noreturn void depth_copy_fail(", "static int copy_depth_stencil("]
    main = r'''
int main(int argc,char **argv) {
    CHECK(argc==2);const char *mode=argv[1];
    VkExec ex={.cb=(VkCommandBuffer)(uintptr_t)17};execution=&ex;
    source_tex=(tex_t){.mem=(uint8_t *)(uintptr_t)0x10000000,.serial=11,
        .d={.format=NVN_FORMAT_DEPTH32F_STENCIL8,.target=NVN_TEXTURE_TARGET_2D,.width=1600,.height=900,.depth=1,.levels=1},
        .L={.size=13107200,.layers=1}};
    destination_tex=source_tex;destination_tex.mem=(uint8_t *)(uintptr_t)0x20000000;destination_tex.serial=12;
    images[0]=(VkTexImage){.image=(VkImage)(uintptr_t)111,.format=VK_FORMAT_D32_SFLOAT_S8_UINT,
        .type=VK_IMAGE_TYPE_2D,.aspect=VK_IMAGE_ASPECT_DEPTH_BIT|VK_IMAGE_ASPECT_STENCIL_BIT,
        .usage=VK_IMAGE_USAGE_TRANSFER_SRC_BIT,.width=1600,.height=900,.depth=1,.levels=1,.layers=1,.samples=1};
    images[1]=images[0];images[1].image=(VkImage)(uintptr_t)222;images[1].usage=VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    p_copy_t2t p={.src=&source_tex,.dst=&destination_tex,.has_sv=1,.has_dv=1,
        .sv={.set_levels=1,.base_level=0,.nlevels=1},.dv={.set_levels=1,.base_level=0,.nlevels=1},
        .sr={0,0,0,1600,900,1},.dr={0,0,0,1600,900,1}};
    uint32_t expected_w=1600,expected_h=900,expected_layers=1;
    if(!strcmp(mode,"array") || !strcmp(mode,"view-layer-bound") || !strcmp(mode,"mip-range") ||
       !strcmp(mode,"layer-base") || !strcmp(mode,"layer-range") || !strcmp(mode,"default-counts")) {
        source_tex.d.target=destination_tex.d.target=NVN_TEXTURE_TARGET_2D_ARRAY;
        source_tex.d.levels=destination_tex.d.levels=images[0].levels=images[1].levels=4;
        source_tex.L.layers=destination_tex.L.layers=images[0].layers=images[1].layers=10;
        p.sv=(view_t){.set_levels=1,.base_level=1,.nlevels=2,.set_layers=1,.min_layer=2,.nlayers=3};
        p.dv=(view_t){.set_levels=1,.base_level=1,.nlevels=1,.set_layers=1,.min_layer=5,.nlayers=4};
        p.sr=(NVNcopyRegion){2,3,1,100,80,2};p.dr=(NVNcopyRegion){7,8,2,100,80,2};
        expected_w=100;expected_h=80;expected_layers=2;
    }
    if(!strcmp(mode,"null-src"))p.src=NULL;
    else if(!strcmp(mode,"null-dst"))p.dst=NULL;
    else if(!strcmp(mode,"colour"))source_tex.d.format=destination_tex.d.format=NVN_FORMAT_RGBA8;
    else if(!strcmp(mode,"format"))destination_tex.d.format=NVN_FORMAT_DEPTH24_STENCIL8;
    else if(!strcmp(mode,"linear"))p.flags=NVN_COPY_FLAGS_LINEAR_FILTER_BIT;
    else if(!strcmp(mode,"engine"))p.flags=NVN_COPY_FLAGS_ENGINE_2D_BIT;
    else if(!strcmp(mode,"mirror"))p.flags=NVN_COPY_FLAGS_MIRROR_X_BIT;
    else if(!strcmp(mode,"unknown-flag"))p.flags=0x10000;
    else if(!strcmp(mode,"mip-32"))p.sv.base_level=32;
    else if(!strcmp(mode,"mip-255"))p.sv.base_level=255;
    else if(!strcmp(mode,"mip-end"))p.sv.base_level=1;
    else if(!strcmp(mode,"mip-range"))p.sv.nlevels=4;
    else if(!strcmp(mode,"layer-base"))p.dv.min_layer=10;
    else if(!strcmp(mode,"layer-range"))p.dv.nlayers=6;
    else if(!strcmp(mode,"view-layer-bound"))p.sv.nlayers=2;
    else if(!strcmp(mode,"negative-x"))p.sr.xoffset=-1;
    else if(!strcmp(mode,"negative-y"))p.sr.yoffset=-1;
    else if(!strcmp(mode,"negative-z"))p.sr.zoffset=-1;
    else if(!strcmp(mode,"negative-width"))p.sr.width=-1;
    else if(!strcmp(mode,"negative-height"))p.sr.height=-1;
    else if(!strcmp(mode,"negative-depth"))p.sr.depth=-1;
    else if(!strcmp(mode,"x-outside"))p.sr.xoffset=1600;
    else if(!strcmp(mode,"y-outside"))p.sr.yoffset=900;
    else if(!strcmp(mode,"z-outside"))p.sr.zoffset=1;
    else if(!strcmp(mode,"region-overflow"))p.sr.xoffset=p.sr.width=INT32_MAX;
    else if(!strcmp(mode,"width"))p.dr.width=1599;
    else if(!strcmp(mode,"height"))p.dr.height=899;
    else if(!strcmp(mode,"layer-count")) {images[0].layers=images[1].layers=2;p.sr.depth=2;}
    else if(!strcmp(mode,"image-format"))images[1].format=VK_FORMAT_D24_UNORM_S8_UINT;
    else if(!strcmp(mode,"missing"))missing=1;
    else if(!strcmp(mode,"zero-handle"))images[0].image=VK_NULL_HANDLE;
    else if(!strcmp(mode,"aspect"))images[1].aspect=VK_IMAGE_ASPECT_DEPTH_BIT;
    else if(!strcmp(mode,"3d"))images[0].type=VK_IMAGE_TYPE_3D;
    else if(!strcmp(mode,"native-3d"))source_tex.d.target=NVN_TEXTURE_TARGET_3D;
    else if(!strcmp(mode,"depth"))images[0].depth=2;
    else if(!strcmp(mode,"samples"))images[0].samples=images[1].samples=4;
    else if(!strcmp(mode,"resolve"))images[0].samples=4;
    else if(!strcmp(mode,"zero-width"))images[0].width=0;
    else if(!strcmp(mode,"zero-height"))images[0].height=0;
    else if(!strcmp(mode,"zero-levels"))images[0].levels=0;
    else if(!strcmp(mode,"over-levels"))images[0].levels=33;
    else if(!strcmp(mode,"zero-layers"))images[0].layers=0;
    else if(!strcmp(mode,"src-usage"))images[0].usage=0;
    else if(!strcmp(mode,"dst-usage"))images[1].usage=0;
    else if(!strcmp(mode,"bound-src"))images[0].bound=1;
    else if(!strcmp(mode,"bound-dst"))images[1].bound=1;
    else if(!strcmp(mode,"same-image"))images[1].image=images[0].image;
    else if(!strcmp(mode,"same-storage"))destination_tex.mem=source_tex.mem;
    else if(!strcmp(mode,"overlap"))destination_tex.mem=source_tex.mem+source_tex.L.size-1;
    else if(!strcmp(mode,"reverse-overlap"))source_tex.mem=destination_tex.mem+1;
    else if(!strcmp(mode,"zero-address"))source_tex.mem=NULL;
    else if(!strcmp(mode,"zero-size"))source_tex.L.size=0;
    else if(!strcmp(mode,"overflow-storage"))destination_tex.mem=(uint8_t *)(uintptr_t)(UINT64_MAX-1024);
    else if(!strcmp(mode,"swizzle"))p.sv.set_swizzle=1;
    else if(!strcmp(mode,"dsmode"))p.sv.set_dsmode=1;
    else if(!strcmp(mode,"view-format"))p.sv.set_format=1,p.sv.format=NVN_FORMAT_DEPTH32F;
    else if(!strcmp(mode,"view-target"))p.sv.set_target=1,p.sv.target=NVN_TEXTURE_TARGET_3D;
    else if(!strcmp(mode,"identity-view")) {
        p.sv.set_format=p.sv.set_target=1;p.sv.format=source_tex.d.format;p.sv.target=source_tex.d.target;
    } else if(!strcmp(mode,"no-view")) {p.has_sv=p.has_dv=0;memset(&p.sv,255,sizeof p.sv);memset(&p.dv,255,sizeof p.dv);}
    else if(!strcmp(mode,"default-counts"))p.sv.nlevels=p.sv.nlayers=p.dv.nlevels=p.dv.nlayers=0;
    else if(!strcmp(mode,"default-region"))p.sr=p.dr=(NVNcopyRegion){0};
    else if(!strcmp(mode,"adjacent-storage"))destination_tex.mem=source_tex.mem+source_tex.L.size;
    else CHECK(!strcmp(mode,"observed") || !strcmp(mode,"array"));
    const p_copy_t2t before=p;const tex_t st=source_tex,dt=destination_tex;
    int handled=copy_depth_stencil(&ex,&p);
    CHECK(!memcmp(&p,&before,sizeof p) && !memcmp(&source_tex,&st,sizeof st) && !memcmp(&destination_tex,&dt,sizeof dt));
    if(!strcmp(mode,"colour")) {CHECK(!handled && !lookups && !copies && !ex.recorded);return 0;}
    CHECK(handled && lookups==2 && pass_ends==1 && barriers==1 && copies==1 && written==1 && ex.recorded==1);
    CHECK(received.srcSubresource.aspectMask==(VK_IMAGE_ASPECT_DEPTH_BIT|VK_IMAGE_ASPECT_STENCIL_BIT));
    CHECK(received.dstSubresource.aspectMask==received.srcSubresource.aspectMask);
    CHECK(received.extent.width==expected_w && received.extent.height==expected_h && received.extent.depth==1);
    CHECK(received.srcSubresource.layerCount==expected_layers && received.dstSubresource.layerCount==expected_layers);
    CHECK(received.srcOffset.z==0 && received.dstOffset.z==0);
    if(!strcmp(mode,"array") || !strcmp(mode,"default-counts")) {
        CHECK(received.srcSubresource.mipLevel==1 && received.dstSubresource.mipLevel==1);
        CHECK(received.srcSubresource.baseArrayLayer==3 && received.dstSubresource.baseArrayLayer==7);
        CHECK(received.srcOffset.x==2 && received.srcOffset.y==3 && received.dstOffset.x==7 && received.dstOffset.y==8);
    } else {
        CHECK(!received.srcSubresource.mipLevel && !received.dstSubresource.mipLevel);
        CHECK(!received.srcSubresource.baseArrayLayer && !received.dstSubresource.baseArrayLayer);
    }
    puts("depth stencil copy passed");return 0;
}
'''
    return preamble + sub + "\n".join(function(xfer, name) for name in funcs) + main


@unittest.skipUnless(os.name == "nt" and CC.is_file() and DEPS.is_dir(), "Windows Clang/Vulkan headers required")
class DepthStencilCopyTests(unittest.TestCase):
    def test_bounded_opt_in_metadata_preserves_inputs(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-depth-xfer-") as tmp:
            path = Path(tmp) / "fixture.c"
            path.write_text(source(), encoding="utf-8")
            for opt in ("-O0", "-O2"):
                exe = path.with_suffix(".exe")
                compile = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", "-march=x86-64-v3",
                                          "-I", str(ROOT / "runtime"), "-I", str(DEPS), "-I", str(DEPS / "vma"),
                                          str(path), "-o", str(exe)],
                                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                self.assertEqual(compile.returncode, 0, compile.stdout + compile.stderr)
                for mode in ("0", "1", "2", ""):
                    with self.subTest(opt=opt, mode=mode):
                        run = subprocess.run([str(exe), mode], capture_output=True, text=True,
                                             encoding="utf-8", errors="replace", timeout=10)
                        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                        self.assertIn("depth transfer diagnostic passed", run.stdout)

    def test_copy_aspects_ranges_order_and_failures(self):
        good = ("observed", "array", "default-counts", "default-region", "identity-view", "no-view", "adjacent-storage", "colour")
        bad = ("null-src", "null-dst", "format", "linear", "engine", "mirror", "unknown-flag",
               "mip-32", "mip-255", "mip-end", "mip-range", "layer-base", "layer-range", "view-layer-bound",
               "negative-x", "negative-y", "negative-z", "negative-width", "negative-height", "negative-depth",
               "x-outside", "y-outside", "z-outside", "region-overflow", "width", "height", "layer-count",
               "image-format", "missing", "zero-handle", "aspect", "3d", "native-3d", "depth", "samples", "resolve",
               "zero-width", "zero-height", "zero-levels", "over-levels", "zero-layers", "src-usage", "dst-usage",
               "bound-src", "bound-dst", "same-image", "same-storage", "overlap", "reverse-overlap", "zero-address",
               "zero-size", "overflow-storage", "swizzle", "dsmode", "view-format", "view-target")
        with tempfile.TemporaryDirectory(prefix="hwaoi-depth-copy-") as tmp:
            path = Path(tmp) / "fixture.c"
            path.write_text(copy_source(), encoding="utf-8")
            for opt in ("-O0", "-O2"):
                exe = path.with_suffix(".exe")
                result = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", "-march=x86-64-v3",
                                         "-I", str(ROOT / "runtime"), "-I", str(DEPS), "-I", str(DEPS / "vma"),
                                         str(path), "-o", str(exe)], capture_output=True, encoding="utf-8", errors="replace", timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for mode in good + bad:
                    with self.subTest(opt=opt, mode=mode):
                        result = subprocess.run([str(exe), mode], capture_output=True, encoding="utf-8", errors="replace", timeout=10)
                        self.assertEqual(result.returncode, 0 if mode in good else 73, result.stdout + result.stderr)
                        if mode in bad:
                            self.assertIn("unsupported depth-stencil T2T", result.stderr)


if __name__ == "__main__":
    unittest.main()
