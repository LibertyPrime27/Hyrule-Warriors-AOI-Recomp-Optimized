"""Synthetic descriptors exercise actual tex_info_full and opt-in observation without changing bindings."""
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
    draw = (ROOT / "runtime/vk_exec_draw.c").read_text()
    # Skip the earlier prototype when extracting the definition.
    full = draw[draw.index("static void tex_info_full(", draw.index("static void tex_info_lookup(")):]
    enums = draw[draw.index("enum { DV_1D,"):draw.index("static struct {", draw.index("enum { DV_1D,"))]
    preamble = r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include "vk_exec.h"
#include "tex.h"
#define CHECK(x) do { if(!(x)) { fprintf(stderr,"check line %d: %s\n",__LINE__,#x);exit(1); } } while(0)
static unsigned logs, lookup_calls, storage_calls;
static int resolve=1, accept=1;
static char line[4096];
static VkTexImage texture={.image=(VkImage)(uintptr_t)88,.addr=0x12340000,.format=VK_FORMAT_R8G8B8A8_UNORM,
    .aspect=VK_IMAGE_ASPECT_COLOR_BIT,.width=32,.height=16,.depth=1,.levels=4,.layers=12,.samples=1,
    .type=VK_IMAGE_TYPE_2D,.view_type=VK_IMAGE_VIEW_TYPE_CUBE_ARRAY,.usage=VK_IMAGE_USAGE_SAMPLED_BIT};
static view_t selected={.set_format=1,.format=37,.set_levels=1,.base_level=2,.nlevels=1,
    .set_layers=1,.min_layer=6,.nlayers=6};
int tex_vk_format(int format,TexVkFormat *out) {
    memset(out,0,sizeof *out);
    if(format==37)out->vk=out->ideal=VK_FORMAT_R8G8B8A8_UNORM;
    else if(format==56)out->vk=out->ideal=VK_FORMAT_R8G8B8A8_SRGB;
    else return 0;
    return 1;
}
void rt_log(const char *fmt,...) {va_list ap;va_start(ap,fmt);vsnprintf(line,sizeof line,fmt,ap);va_end(ap);logs++;}
const VkTexImage *vk_texture_by_id_locked(const dpool_ref *pool,int id,view_t *v,int *hv) {
    CHECK(pool->addr==0x10000 && pool->count==16 && id==5);lookup_calls++;
    *v=selected;*hv=1;return resolve ? &texture : NULL;
}
static VkImageView get_view(VkExec *ex,const VkTexImage *img,const view_t *v,int hv,uint16_t type,int storage) {
    (void)ex;(void)type;(void)storage;CHECK(img==&texture && hv && !memcmp(v,&selected,sizeof *v));
    return accept ? (VkImageView)(uintptr_t)99 : VK_NULL_HANDLE;
}
void vk_texture_storage_bound_locked(const VkTexImage *img) {CHECK(img==&texture);storage_calls++;}
static int in_fbl(const VkExec *ex,VkImage image) {(void)ex;CHECK(image==texture.image);return 1;}
VkSampler vk_sampler_point_by_id_locked(const dpool_ref *pool,int id) {(void)pool;CHECK(id==3);return (VkSampler)(uintptr_t)44;}
VkSampler vk_sampler_by_id_locked(const dpool_ref *pool,int id) {(void)pool;CHECK(id==3);return (VkSampler)(uintptr_t)33;}
'''
    body = r'''
static struct {
    VkSampler samp,samp_shadow;
    struct {VkImageView view;} img[DV_COUNT];
} Z;
static VkImageView storage_dummy(int index,int code) {(void)index;(void)code;return (VkImageView)(uintptr_t)777;}
'''
    main = r'''
int main(int argc,char **argv) {
    CHECK(argc==2);const char *mode=argv[1];
    _putenv_s("HWAOI_IMAGE_DIAG",strstr(mode,"off") ? "0" : "1");
    Z.samp=(VkSampler)(uintptr_t)11;Z.samp_shadow=(VkSampler)(uintptr_t)22;
    for(int i=0;i<DV_COUNT;i++)Z.img[i].view=(VkImageView)(uintptr_t)(100+i);
    VkExec ex={.tex_pool={.addr=0x10000,.count=16}};
    GfxShaderRes r={.binding=18,.slot=8,.handle=24,.type=0x505};
    uint64_t h=(3<<20)|5;int storage=0,separate=0;
    if(!strcmp(mode,"unbound"))h=0;
    else if(!strcmp(mode,"missing"))resolve=0;
    else if(!strcmp(mode,"rejected-view"))accept=0;
    else if(!strcmp(mode,"depth"))texture.aspect=VK_IMAGE_ASPECT_DEPTH_BIT;
    else if(!strcmp(mode,"ordinary-depth"))texture.aspect=VK_IMAGE_ASPECT_DEPTH_BIT,r.type=0x105;
    else if(!strcmp(mode,"storage"))storage=1;
    else if(!strcmp(mode,"separate"))separate=1;
    else if(!strncmp(mode,"format",6))r.type=0x105,selected.format=!strcmp(mode,"format-unknown")?255:56;
    else CHECK(!strcmp(mode,"off") || !strcmp(mode,"colour") || !strcmp(mode,"limit"));
    const VkTexImage expected_texture=texture;const view_t expected_view=selected;
    VkDescriptorImageInfo out;VkImage image;
    tex_info_full(&ex,&r,h,storage,separate,&out,&image);
    CHECK(!memcmp(&texture,&expected_texture,sizeof texture) && !memcmp(&selected,&expected_view,sizeof selected));
    int fallback=!h || !resolve || !accept;
    CHECK(out.imageView==(VkImageView)(uintptr_t)(fallback ? 100+DV_CUBE_ARRAY : 99));
    CHECK(out.sampler==(VkSampler)(uintptr_t)((storage||separate)?0:!h?22:!strcmp(mode,"ordinary-depth")?44:33));
    CHECK(image==((h&&resolve)?texture.image:VK_NULL_HANDLE) && out.imageLayout==VK_IMAGE_LAYOUT_GENERAL);
    CHECK(lookup_calls==(h?1u:0u) && storage_calls==(storage?1u:0u));
    int expect_log=strcmp(mode,"off") && !storage && (r.type&0x400) &&
        (fallback || !(texture.aspect&VK_IMAGE_ASPECT_DEPTH_BIT));
    int format_log=!strncmp(mode,"format",6) && !strstr(mode,"off");
    CHECK(logs==(unsigned)(expect_log+format_log));
    if(format_log) {
        CHECK(strstr(line,"image-diag: sampled-format binding=18 slot=8"));
        CHECK(strstr(line,"format_policy=explicit-compatible"));
        CHECK(strstr(line,!strcmp(mode,"format-unknown")?"requested_known=0":"requested_known=1"));
        for(int i=0;i<100;i++)tex_info_full(&ex,&r,h,storage,separate,&out,&image);
        CHECK(logs==1 && out.imageView==(VkImageView)(uintptr_t)99 && out.sampler==(VkSampler)(uintptr_t)33);
        if(!strcmp(mode,"format-limit")) {
            for(int i=1;i<160;i++) {
                texture.image=(VkImage)(uintptr_t)(88+i);
                tex_info_full(&ex,&r,h,storage,separate,&out,&image);
            }
            CHECK(logs==129 && strstr(line,"observation limit reached"));
            CHECK(out.imageView==(VkImageView)(uintptr_t)99 && out.sampler==(VkSampler)(uintptr_t)33);
        }
    }
    if(expect_log) {
        CHECK(strstr(line,"binding=18 slot=8 shader_handle=0x18 type=0x505"));
        CHECK(strstr(line,fallback?"fallback=1":"fallback=0"));
        CHECK(strstr(line,(h&&resolve)?"resolved=1":"resolved=0"));
        CHECK(strstr(line,"pool=0x10000/16"));
        if(h&&resolve)CHECK(strstr(line,"shape=32x16x1 levels=4 layers=12"));
    }
    if(!strcmp(mode,"limit")) {
        for(int i=0;i<100;i++)tex_info_full(&ex,&r,h,storage,separate,&out,&image);
        CHECK(logs==64 && out.imageView==(VkImageView)(uintptr_t)99 && out.sampler==(VkSampler)(uintptr_t)33);
    }
    puts("image binding diagnostic check passed");return 0;
}
'''
    return (preamble + enums + body + function(draw, "static VkImageViewType res_view_type(") +
            function(draw, "static int dummy_index(") + function(draw, "VkImageAspectFlags vk_exec_view_aspect(") +
            function(draw, "static void sampled_format_diag(") + function(draw, "static void image_binding_diag(") +
            function(full, "static void tex_info_full(") + main)


@unittest.skipUnless(os.name == "nt" and CC.is_file() and DEPS.is_dir(), "Windows Clang and Vulkan headers required")
class ImageBindingDiagnosticTests(unittest.TestCase):
    def test_actual_binding_is_preserved_with_bounded_opt_in_diagnostics(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-image-diag-") as tmp:
            path = Path(tmp) / "fixture.c"
            path.write_text(source(), encoding="utf-8")
            for opt in ("-O0", "-O2"):
                exe = path.with_name(f"fixture{opt}.exe")
                result = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", "-march=x86-64-v3",
                    "-I", str(ROOT / "runtime"), "-I", str(DEPS), "-I", str(DEPS / "vma"), str(path), "-o", str(exe)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                for case in ("off", "colour", "unbound", "missing", "rejected-view", "depth", "ordinary-depth",
                             "storage", "separate", "limit", "format", "format-off", "format-unknown", "format-limit"):
                    with self.subTest(opt=opt,case=case):
                        result=subprocess.run([str(exe),case],capture_output=True,text=True,timeout=10)
                        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                        self.assertIn("check passed",result.stdout)


if __name__ == "__main__":
    unittest.main()
