"""Actual Vulkan create/view policies with synthetic device capabilities; no GPU or game inputs."""
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
    tex = (ROOT / "runtime/vk_texture.c").read_text()
    start = draw.index("typedef struct {\n    VkImage image;\n    VkFormat format;")
    key = draw[start:draw.index("typedef struct { uint64_t addr, size; } Range;", start)]
    start = draw.index("typedef struct ViewNode")
    nodes = draw[start:draw.index("// An image view of a registered texture", start)]
    preamble = r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include "tex.h"
#include "vk_exec.h"
#include "nvn_hmap.h"
#define CHECK(x) do { if(!(x)) { fprintf(stderr,"check line %d: %s\n",__LINE__,#x);exit(1); } } while(0)
static VkCtx context;
static int storage_feature=1, sampled_feature=1, shape_supported=1, view_success=1;
static unsigned queries, creates;
static VkPhysicalDeviceImageFormatInfo2 queried;
static VkImageViewCreateInfo created;
static VkImageUsageFlags created_usage;
static VkImageFormatProperties caps={.maxExtent={64,64,16},.maxMipLevels=4,.maxArrayLayers=8,
    .sampleCounts=VK_SAMPLE_COUNT_1_BIT,.maxResourceSize=UINT64_MAX};
VkCtx *vk_ctx(void) {return &context;}
void rt_fatal(const char *fmt,...) {va_list ap;va_start(ap,fmt);vfprintf(stderr,fmt,ap);va_end(ap);exit(73);}
int vk_astc_native(VkFormat f) {(void)f;return 0;}
int vk_astc_bc(void) {return 0;}
void vk_texture_lock(void) {}
void vk_texture_unlock(void) {}
VKAPI_ATTR void VKAPI_CALL vkGetPhysicalDeviceFormatProperties(VkPhysicalDevice device,VkFormat f,VkFormatProperties *p) {
    (void)device;memset(p,0,sizeof *p);
    p->optimalTilingFeatures=(sampled_feature?VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT:0)|VK_FORMAT_FEATURE_TRANSFER_SRC_BIT|
        VK_FORMAT_FEATURE_TRANSFER_DST_BIT|VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT;
    if(f==VK_FORMAT_R8G8B8A8_UNORM && storage_feature)p->optimalTilingFeatures|=VK_FORMAT_FEATURE_STORAGE_IMAGE_BIT;
}
VKAPI_ATTR VkResult VKAPI_CALL vkGetPhysicalDeviceImageFormatProperties2(VkPhysicalDevice device,
    const VkPhysicalDeviceImageFormatInfo2 *q,VkImageFormatProperties2 *p) {
    (void)device;queries++;queried=*q;p->imageFormatProperties=caps;
    return shape_supported ? VK_SUCCESS : VK_ERROR_FORMAT_NOT_SUPPORTED;
}
VKAPI_ATTR VkResult VKAPI_CALL vkCreateImageView(VkDevice device,const VkImageViewCreateInfo *ci,
    const VkAllocationCallbacks *allocator,VkImageView *view) {
    (void)device;(void)allocator;creates++;created=*ci;
    CHECK(ci->pNext);const VkImageViewUsageCreateInfo *u=ci->pNext;
    CHECK(u->sType==VK_STRUCTURE_TYPE_IMAGE_VIEW_USAGE_CREATE_INFO);created_usage=u->usage;
    CHECK(ci->format!=VK_FORMAT_R8G8B8A8_SRGB || !(u->usage & VK_IMAGE_USAGE_STORAGE_BIT));
    *view=(VkImageView)(uintptr_t)creates;return view_success ? VK_SUCCESS : VK_ERROR_FORMAT_NOT_SUPPORTED;
}
'''
    main = r'''
int main(int argc,char **argv) {
    CHECK(argc==2);const char *mode=argv[1];
    TexVkFormat srgb,linear;CHECK(tex_vk_format(56,&srgb) && tex_vk_format(37,&linear));
    CHECK(srgb.vk==VK_FORMAT_R8G8B8A8_SRGB && linear.vk==VK_FORMAT_R8G8B8A8_UNORM);
    CHECK(srgb.vk==srgb.ideal && linear.vk==linear.ideal && !srgb.expand_rgb && !linear.expand_rgb);
    VkImageCreateInfo base={.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
        .flags=VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT|VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT,
        .imageType=VK_IMAGE_TYPE_2D,.format=srgb.vk,.extent={32,32,1},.mipLevels=4,.arrayLayers=6,
        .samples=VK_SAMPLE_COUNT_1_BIT,.tiling=VK_IMAGE_TILING_OPTIMAL,
        .usage=VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT|
            VK_IMAGE_USAGE_TRANSFER_DST_BIT|VK_IMAGE_USAGE_ATTACHMENT_FEEDBACK_LOOP_BIT_EXT};
    if(!strcmp(mode,"capabilities")) {
        VkImageCreateInfo ci=base;
        CHECK(enable_srgb_storage(&ci,56,&srgb));
        CHECK(ci.format==srgb.vk && ci.usage==(base.usage|VK_IMAGE_USAGE_STORAGE_BIT));
        CHECK(ci.flags==(base.flags|VK_IMAGE_CREATE_EXTENDED_USAGE_BIT));
        CHECK(queries==1 && queried.flags==ci.flags && queried.usage==ci.usage && queried.format==ci.format);
        CHECK(queried.type==ci.imageType && queried.tiling==ci.tiling);
        for(int failure=0;failure<10;failure++) {
            ci=base;storage_feature=shape_supported=1;caps.maxExtent.width=64;caps.maxExtent.height=64;
            caps.maxExtent.depth=16;caps.maxMipLevels=4;caps.maxArrayLayers=8;caps.sampleCounts=VK_SAMPLE_COUNT_1_BIT;
            TexVkFormat f=srgb;int nvn=56;
            switch(failure) {
            case 0: storage_feature=0;break;
            case 1: shape_supported=0;break;
            case 2: caps.maxExtent.width=16;break;
            case 3: caps.maxExtent.height=16;break;
            case 4: caps.maxMipLevels=3;break;
            case 5: caps.maxArrayLayers=5;break;
            case 6: caps.sampleCounts=VK_SAMPLE_COUNT_4_BIT;break;
            case 7: ci.samples=VK_SAMPLE_COUNT_4_BIT;break;
            case 8: nvn=NVN_FORMAT_RGBA8;break;
            case 9: f.ideal=VK_FORMAT_ASTC_4x4_SRGB_BLOCK;break;
            }
            VkImageCreateInfo expected=ci;
            CHECK(!enable_srgb_storage(&ci,nvn,&f));CHECK(!memcmp(&ci,&expected,sizeof ci));
        }
        puts("sRGB capabilities check passed");return 0;
    }
    VkTexImage img={.image=(VkImage)(uintptr_t)100,.format=srgb.vk,.type=VK_IMAGE_TYPE_2D,
        .view_type=VK_IMAGE_VIEW_TYPE_2D_ARRAY,.samples=1,.levels=4,.layers=6,
        .aspect=VK_IMAGE_ASPECT_COLOR_BIT,.usage=base.usage|VK_IMAGE_USAGE_STORAGE_BIT,.create_flags=base.flags,
        .swz={NVN_TEXTURE_SWIZZLE_R,NVN_TEXTURE_SWIZZLE_G,NVN_TEXTURE_SWIZZLE_B,NVN_TEXTURE_SWIZZLE_A}};
    view_t v={.set_format=1,.format=NVN_FORMAT_RGBA8,.set_levels=1,.base_level=2,.nlevels=1,
        .set_layers=1,.min_layer=3,.nlayers=2};
    if(!strncmp(mode,"sampled-",8)) {
        VkFormat want=linear.vk;
        if(!strcmp(mode,"sampled-rgba-srgb")) img.format=linear.vk,v.format=NVN_FORMAT_RGBA8_SRGB,want=srgb.vk;
        else if(!strcmp(mode,"sampled-bc1")) img.format=VK_FORMAT_BC1_RGBA_UNORM_BLOCK,v.format=NVN_FORMAT_RGBA_DXT1_SRGB,want=VK_FORMAT_BC1_RGBA_SRGB_BLOCK;
        else if(!strcmp(mode,"sampled-bc3")) img.format=VK_FORMAT_BC3_UNORM_BLOCK,v.format=NVN_FORMAT_RGBA_DXT5_SRGB,want=VK_FORMAT_BC3_SRGB_BLOCK;
        else if(!strcmp(mode,"sampled-bc7")) img.format=VK_FORMAT_BC7_UNORM_BLOCK,v.format=NVN_FORMAT_BPTC_UNORM_SRGB,want=VK_FORMAT_BC7_SRGB_BLOCK;
        else if(!strcmp(mode,"sampled-immutable")) img.create_flags=0;
        else if(!strcmp(mode,"sampled-no-feature")) sampled_feature=0;
        else if(!strcmp(mode,"sampled-no-usage")) img.usage &= ~VK_IMAGE_USAGE_SAMPLED_BIT;
        else if(!strcmp(mode,"sampled-depth")) img.aspect=VK_IMAGE_ASPECT_DEPTH_BIT;
        else if(!strcmp(mode,"sampled-ms")) img.samples=4;
        else if(!strcmp(mode,"sampled-unobserved-bc2")) img.format=VK_FORMAT_BC2_UNORM_BLOCK,v.format=NVN_FORMAT_RGBA_DXT3_SRGB;
        else if(!strcmp(mode,"sampled-cross-class")) img.format=VK_FORMAT_BC1_RGBA_UNORM_BLOCK,v.format=NVN_FORMAT_RGBA_DXT5_SRGB;
        else if(!strcmp(mode,"sampled-unknown")) v.format=0xffff;
        else if(!strcmp(mode,"sampled-fallback")) v.format=NVN_FORMAT_RGBA_ASTC_4x4;
        else if(!strcmp(mode,"sampled-alpha-remap")) img.alpha_one=1;
        else if(!strcmp(mode,"sampled-creation-failure")) view_success=0;
        else CHECK(!strcmp(mode,"sampled-rgba-linear"));
        const VkTexImage original=img;const view_t original_view=v;
        VkImageView view=get_view(NULL,&img,&v,1,0x102,0);
        CHECK(view && creates==1 && created.format==want && created.image==img.image);
        CHECK(created_usage==(VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_ATTACHMENT_FEEDBACK_LOOP_BIT_EXT));
        CHECK(created.subresourceRange.baseMipLevel==2 && created.subresourceRange.levelCount==1);
        CHECK(created.subresourceRange.baseArrayLayer==3 && created.subresourceRange.layerCount==2);
        CHECK(created.components.r==VK_COMPONENT_SWIZZLE_R && created.components.a==VK_COMPONENT_SWIZZLE_A);
        CHECK(!memcmp(&img,&original,sizeof img) && !memcmp(&v,&original_view,sizeof v));
        CHECK(get_view(NULL,&img,&v,1,0x102,0)==view && creates==1);
        // A base-format view must not alias this differently interpreted view.
        CHECK(get_view(NULL,&img,NULL,0,0x102,0)!=view && creates==2 && created.format==img.format);
        puts("sampled format check passed");return 0;
    }
    if(!strcmp(mode,"no-storage-usage")) img.usage &= ~VK_IMAGE_USAGE_STORAGE_BIT;
    else if(!strcmp(mode,"no-storage-feature")) storage_feature=0;
    else if(!strcmp(mode,"unsupported-format")) v.format=NVN_FORMAT_R8;
    else if(!strcmp(mode,"fallback-format")) v.format=NVN_FORMAT_RGBA_ASTC_4x4;
    else if(!strcmp(mode,"srgb-storage")) v.format=NVN_FORMAT_RGBA8_SRGB;
    else if(!strcmp(mode,"creation-failure")) view_success=0;
    else CHECK(!strcmp(mode,"views"));
    VkImageView storage=get_view(NULL,&img,&v,1,0x102,1);
    CHECK(storage && created.format==VK_FORMAT_R8G8B8A8_UNORM && created_usage==VK_IMAGE_USAGE_STORAGE_BIT);
    CHECK(created.subresourceRange.baseMipLevel==2 && created.subresourceRange.levelCount==1);
    CHECK(created.subresourceRange.baseArrayLayer==3 && created.subresourceRange.layerCount==2);
    CHECK(created.components.r==VK_COMPONENT_SWIZZLE_R && created.components.a==VK_COMPONENT_SWIZZLE_A);
    CHECK(get_view(NULL,&img,&v,1,0x102,1)==storage && creates==1);
    VkImageView sampled=get_view(NULL,&img,&v,1,0x102,0);
    CHECK(sampled!=storage && created.format==linear.vk);
    CHECK(created_usage==(VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_ATTACHMENT_FEEDBACK_LOOP_BIT_EXT));
    // Same image, format, range and swizzle but different usage must never reuse the other view.
    img.image=(VkImage)(uintptr_t)101;img.format=linear.vk;
    storage=get_view(NULL,&img,&v,1,0x102,1);unsigned before=creates;
    sampled=get_view(NULL,&img,&v,1,0x102,0);
    CHECK(sampled!=storage && creates==before+1 && created.format==linear.vk);
    CHECK(!(created_usage & VK_IMAGE_USAGE_STORAGE_BIT));
    CHECK(get_view(NULL,&img,&v,1,0x102,1)==storage && creates==before+1);
    CHECK(get_view(NULL,&img,&v,1,0x102,0)==sampled && creates==before+1);
    // Equal-format sRGB attachment must also drop inherited storage usage, preserving other allowed usages.
    img.image=(VkImage)(uintptr_t)100;img.format=srgb.vk;
    CHECK(vk_exec_target_view(&img,srgb.vk,VK_IMAGE_VIEW_TYPE_2D_ARRAY,VK_IMAGE_ASPECT_COLOR_BIT,2,3,2,0));
    CHECK(created.format==srgb.vk && created_usage==base.usage);
    CHECK(created.subresourceRange.levelCount==1 && created.subresourceRange.baseArrayLayer==3);
    before=creates;
    CHECK(vk_exec_target_view(&img,srgb.vk,VK_IMAGE_VIEW_TYPE_2D_ARRAY,VK_IMAGE_ASPECT_COLOR_BIT,2,3,2,0));
    CHECK(creates==before);
    puts("sRGB views check passed");return 0;
}
'''
    return (preamble + key + nodes + function(tex, "static VkFormatFeatureFlags features_of(") +
            function(tex, "static int block_views_ok(") + function(tex, "static int enable_srgb_storage(") +
            function(draw, "static VkImageViewType res_view_type(") + function(draw, "static VkComponentSwizzle swz_of(") +
            function(draw, "VkImageAspectFlags vk_exec_view_aspect(") + function(draw, "static int sampled_format_compatible(") +
            function(draw, "static VkImageView get_view(") +
            function(draw, "VkImageView vk_exec_target_view(") + main)


@unittest.skipUnless(os.name == "nt" and CC.is_file() and DEPS.is_dir(), "Windows Clang and Vulkan headers required")
class SrgbImageViewTests(unittest.TestCase):
    def test_actual_creation_view_and_cache_paths(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-srgb-view-") as tmp:
            path = Path(tmp) / "fixture.c"
            path.write_text(source(), encoding="utf-8")
            for opt in ("-O0", "-O2"):
                exe = path.with_name(f"fixture{opt}.exe")
                result = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers",
                    "-march=x86-64-v3",
                    "-I", str(ROOT / "runtime"), "-I", str(DEPS), "-I", str(DEPS / "vma"), str(path),
                    str(ROOT / "runtime/vk_formats.c"), "-o", str(exe)], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for case in ("capabilities", "views", "no-storage-usage", "no-storage-feature", "unsupported-format",
                             "fallback-format", "srgb-storage", "creation-failure",
                             "sampled-rgba-linear", "sampled-rgba-srgb", "sampled-bc1", "sampled-bc3", "sampled-bc7",
                             "sampled-immutable", "sampled-no-feature", "sampled-no-usage", "sampled-depth", "sampled-ms",
                             "sampled-unobserved-bc2", "sampled-cross-class", "sampled-unknown", "sampled-fallback",
                             "sampled-alpha-remap", "sampled-creation-failure"):
                    with self.subTest(opt=opt, case=case):
                        result = subprocess.run([str(exe), case], capture_output=True, text=True,
                                                encoding="utf-8", errors="replace", timeout=10)
                        good = case in ("capabilities", "views", "sampled-rgba-linear", "sampled-rgba-srgb",
                                        "sampled-bc1", "sampled-bc3", "sampled-bc7")
                        self.assertEqual(result.returncode, 0 if good else 73, result.stdout + result.stderr)
                        self.assertIn("check passed" if good else "vk:", result.stdout if good else result.stderr)


if __name__ == "__main__":
    unittest.main()
