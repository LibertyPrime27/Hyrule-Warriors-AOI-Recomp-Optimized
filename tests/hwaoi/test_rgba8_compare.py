"""Real texture-layout reads, exact UNORM conversion, immutable Vulkan upload/view versions, and bind integration."""
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
from tests.hwaoi.test_srgb_image_view import source as view_source


def source():
    draw = (ROOT / "runtime/vk_exec_draw.c").read_text()
    tex = (ROOT / "runtime/vk_texture.c").read_text()
    base = view_source().split("\nint main(int argc,char **argv)")[0]
    start = tex.index("typedef struct {\n    uint64_t addr;")
    types = tex[start:tex.index("// A slot keeps", start)]
    start = draw.index("typedef struct Rgba8Compare")
    conversion = draw[start:draw.index("// ---- program to shaders", start)]
    stubs = r'''
#include "platform/platform.h"
#include "nvn_rgba8_compare.h"
static Tex source_tex;
static uint8_t *storage_base;
static size_t storage_bytes;
static int readable=1, depth_feature=1, linear_feature=1, allocation_ok=1;
static unsigned allocations, destroyed_images, destroyed_views, uploads, upload_step;
static uint16_t staged[NVN_RGBA8_COMPARE_MAX_TEXELS], copied[NVN_RGBA8_COMPARE_MAX_TEXELS];
static VkImageCreateInfo allocated;
static view_t source_view={.set_format=1,.format=NVN_FORMAT_RGBA8,
    .set_levels=1,.nlevels=1,.set_layers=1,.nlayers=6};
static samp_par source_sampler={.cmp_mode=NVN_COMPARE_MODE_COMPARE_R_TO_TEXTURE,
    .ws=NVN_WRAP_MODE_CLAMP_TO_EDGE,.wt=NVN_WRAP_MODE_CLAMP_TO_EDGE,.wr=NVN_WRAP_MODE_CLAMP_TO_EDGE};
static int sampler_present=1;
int vk_sampler_params_locked(const dpool_ref *pool,int id,samp_par *out) {
    (void)pool;CHECK(id==0);if(sampler_present)*out=source_sampler;return sampler_present;
}
const char *cfg_get(const char *key) {(void)key;return NULL;}
int plat_mem_readable(uint64_t addr,uint64_t size) {
    return readable && addr==(uintptr_t)storage_base && size==storage_bytes;
}
VKAPI_ATTR void VKAPI_CALL vkGetPhysicalDeviceFormatProperties2(VkPhysicalDevice device,VkFormat format,VkFormatProperties2 *p) {
    (void)device;CHECK(format==VK_FORMAT_D16_UNORM);
    VkFormatProperties3 *f=(VkFormatProperties3 *)p->pNext;CHECK(f && f->sType==VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_3);
    f->optimalTilingFeatures=VK_FORMAT_FEATURE_2_SAMPLED_IMAGE_BIT|VK_FORMAT_FEATURE_2_TRANSFER_DST_BIT;
    if(depth_feature)f->optimalTilingFeatures|=VK_FORMAT_FEATURE_2_SAMPLED_IMAGE_DEPTH_COMPARISON_BIT;
    if(linear_feature)f->optimalTilingFeatures|=VK_FORMAT_FEATURE_2_SAMPLED_IMAGE_FILTER_LINEAR_BIT;
}
VkResult vmaCreateImage(VmaAllocator allocator,const VkImageCreateInfo *ci,const VmaAllocationCreateInfo *ai,
                       VkImage *image,VmaAllocation *allocation,VmaAllocationInfo *info) {
    (void)allocator;(void)ai;(void)info;allocated=*ci;allocations++;
    CHECK(ci->format==VK_FORMAT_D16_UNORM && ci->extent.width==source_tex.pub.width &&
          ci->extent.height==source_tex.pub.height && ci->extent.depth==1);
    CHECK(ci->mipLevels==1 && ci->samples==VK_SAMPLE_COUNT_1_BIT &&
          ci->usage==(VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT));
    CHECK(ci->usage==queried.usage && ci->flags==queried.flags && ci->format==queried.format);
    *image=(VkImage)(uintptr_t)(1000+allocations);*allocation=(VmaAllocation)(uintptr_t)(2000+allocations);
    return allocation_ok ? VK_SUCCESS : VK_ERROR_OUT_OF_DEVICE_MEMORY;
}
void vmaDestroyImage(VmaAllocator allocator,VkImage image,VmaAllocation allocation) {
    (void)allocator;(void)image;(void)allocation;destroyed_images++;
}
VKAPI_ATTR void VKAPI_CALL vkDestroyImageView(VkDevice device,VkImageView view,const VkAllocationCallbacks *allocator) {
    (void)device;(void)view;(void)allocator;destroyed_views++;
}
VkCommandBuffer vk_upload_begin(void) {CHECK(!upload_step);upload_step=1;return (VkCommandBuffer)(uintptr_t)333;}
void *vk_staging(VkDeviceSize size,VkBuffer *buffer,VmaAllocation *allocation) {
    CHECK(upload_step==1 && size<=sizeof staged && !allocation);*buffer=(VkBuffer)(uintptr_t)444;return staged;
}
VKAPI_ATTR void VKAPI_CALL vkCmdPipelineBarrier2(VkCommandBuffer cb,const VkDependencyInfo *di) {
    CHECK(cb==(VkCommandBuffer)(uintptr_t)333 && di->imageMemoryBarrierCount==1);
    const VkImageMemoryBarrier2 *b=di->pImageMemoryBarriers;
    CHECK(b->subresourceRange.aspectMask==VK_IMAGE_ASPECT_DEPTH_BIT && b->subresourceRange.levelCount==1 &&
          b->subresourceRange.layerCount==source_tex.pub.layers && b->newLayout==VK_IMAGE_LAYOUT_GENERAL);
    if(upload_step==1) {CHECK(b->oldLayout==VK_IMAGE_LAYOUT_UNDEFINED && b->dstAccessMask==VK_ACCESS_2_TRANSFER_WRITE_BIT);upload_step=2;}
    else {CHECK(upload_step==3 && b->srcAccessMask==VK_ACCESS_2_TRANSFER_WRITE_BIT &&
                b->dstAccessMask==VK_ACCESS_2_SHADER_SAMPLED_READ_BIT);upload_step=4;}
}
VKAPI_ATTR void VKAPI_CALL vkCmdCopyBufferToImage(VkCommandBuffer cb,VkBuffer buffer,VkImage image,VkImageLayout layout,
    uint32_t count,const VkBufferImageCopy *r) {
    (void)image;CHECK(upload_step==2 && cb==(VkCommandBuffer)(uintptr_t)333 && buffer==(VkBuffer)(uintptr_t)444);
    CHECK(layout==VK_IMAGE_LAYOUT_GENERAL && count==1 && !r->bufferOffset && !r->imageSubresource.baseArrayLayer);
    CHECK(r->imageSubresource.aspectMask==VK_IMAGE_ASPECT_DEPTH_BIT && !r->imageSubresource.mipLevel &&
          r->imageSubresource.layerCount==source_tex.pub.layers && r->imageExtent.width==source_tex.pub.width &&
          r->imageExtent.height==source_tex.pub.height);
    memcpy(copied,staged,source_tex.pub.width*source_tex.pub.height*source_tex.pub.layers*sizeof *copied);upload_step=3;
}
void vk_upload_end(void) {CHECK(upload_step==4);upload_step=0;uploads++;}
void rt_log(const char *fmt,...) {
    va_list ap;va_start(ap,fmt);vfprintf(stderr,fmt,ap);va_end(ap);fputc('\n',stderr);
}
static void tex_info_lookup(VkExec *ex,const GfxShaderRes *r,uint64_t h,int storage,int separate,VkDescriptorImageInfo *out) {
    (void)ex;(void)r;(void)h;(void)storage;(void)separate;
    *out=(VkDescriptorImageInfo){.sampler=(VkSampler)(uintptr_t)123,.imageView=(VkImageView)(uintptr_t)88,
                               .imageLayout=VK_IMAGE_LAYOUT_ATTACHMENT_FEEDBACK_LOOP_OPTIMAL_EXT};
}
const VkTexImage *vk_texture_by_id_locked(const dpool_ref *pool,int id,view_t *v,int *hv) {
    (void)pool;CHECK(id==5);if(v)*v=source_view;if(hv)*hv=1;return &source_tex.pub;
}
typedef struct {VkImageView view[2];} Twin;
static int twin_dispatch;
static Twin *twin_update(VkExec *ex,const VkTexImage *img) {(void)ex;(void)img;return NULL;}
'''
    main = r'''
static void pixel(unsigned layer,uint8_t value) {
    uint8_t rgba[64][4];unsigned area=source_tex.pub.width*source_tex.pub.height;
    CHECK(area<=64);
    for(unsigned i=0;i<area;i++)memset(rgba[i],(uint8_t)(value+i*37),4);
    CHECK(!nvn_swizzle(&source_tex.t.d,0,layer,rgba,source_tex.pub.width*4,source_tex.t.mem));
}
static int compare_ref(int64_t ref,int64_t value,int op) {
    switch(op) {
    case 0:return 0;case 1:return ref<value;case 2:return ref==value;case 3:return ref<=value;
    case 4:return ref>value;case 5:return ref!=value;case 6:return ref>=value;default:return 1;
    }
}
// Independent component-selection oracle: BGR keeps equal source channels, and keeps (result,result,result,1).
// Exact rational comparisons exercise all UNORM values and the equality boundary without float-rounding ambiguity.
static void comparison_equivalence(void) {
    VkTexImage img={.swz={2,3,4,5}};view_t view={.set_swizzle=1,.swz={4,3,2,5}};uint8_t effective[4];
    CHECK(rgba8_compare_mapping(&img,&view,1,effective)==2);
    for(int v=0;v<256;v++) {
        uint8_t rgba[6][4]={{0}};uint16_t d[6];memset(rgba[0],v,4);
        CHECK(nvn_rgba8_compare_depth(rgba,1,d));
        const int refs[5]={0,v*4-1,v*4,v*4+1,1020};
        for(int j=0;j<5;j++)for(int op=0;op<8;op++) {
            const int ref=refs[j]<0?0:refs[j]>1020?1020:refs[j];
            int raw=compare_ref(ref,rgba[0][0]*4,op);
            int selected=compare_ref(ref,rgba[0][effective[0]-2]*4,op);
            int converted=compare_ref((int64_t)ref*65535,(int64_t)d[0]*1020,op);
            const int native_result[4]={raw,raw,raw,1};
            CHECK(raw==selected && selected==converted && native_result[effective[0]-2]==converted);
        }
    }
    // Enumerate all selector quadruples: constants, duplicates, alpha swaps and other permutations stay rejected.
    for(unsigned bits=0;bits<4096;bits++) {
        for(int i=0;i<4;i++)view.swz[i]=(bits>>(3*i))&7;
        int expected=!memcmp(view.swz,(uint8_t[]){2,3,4,5},4)?1:
                     !memcmp(view.swz,(uint8_t[]){4,3,2,5},4)?2:0;
        CHECK(rgba8_compare_mapping(&img,&view,1,effective)==expected);
    }
    uint8_t rgba[NVN_RGBA8_COMPARE_MAX_TEXELS][4];
    uint16_t guarded[NVN_RGBA8_COMPARE_MAX_TEXELS+2];
    for(unsigned i=0;i<NVN_RGBA8_COMPARE_MAX_TEXELS;i++)memset(rgba[i],(uint8_t)i,4);
    memset(guarded,0xa5,sizeof guarded);
    CHECK(nvn_rgba8_compare_count(8,8,6)==384 && !nvn_rgba8_compare_count(0,8,1) &&
          !nvn_rgba8_compare_count(8,9,1) && !nvn_rgba8_compare_count(UINT32_MAX,8,1) &&
          !nvn_rgba8_compare_count(8,8,UINT32_MAX));
    CHECK(nvn_rgba8_compare_texels(rgba,NVN_RGBA8_COMPARE_MAX_TEXELS,guarded+1));
    for(unsigned i=0;i<NVN_RGBA8_COMPARE_MAX_TEXELS;i++)CHECK(guarded[i+1]==(uint8_t)i*257u);
    CHECK(guarded[0]==0xa5a5 && guarded[NVN_RGBA8_COMPARE_MAX_TEXELS+1]==0xa5a5);
    memset(guarded,0xa5,sizeof guarded);rgba[NVN_RGBA8_COMPARE_MAX_TEXELS-1][3]^=1;
    CHECK(!nvn_rgba8_compare_texels(rgba,NVN_RGBA8_COMPARE_MAX_TEXELS,guarded+1));
    CHECK(!nvn_rgba8_compare_texels(rgba,0,guarded+1));
    CHECK(!nvn_rgba8_compare_texels(rgba,NVN_RGBA8_COMPARE_MAX_TEXELS+1,guarded+1));
    for(unsigned i=0;i<NVN_RGBA8_COMPARE_MAX_TEXELS+2;i++)CHECK(guarded[i]==0xa5a5);
}
int main(int argc,char **argv) {
    CHECK(argc==2);const char *mode=argv[1];
    comparison_equivalence();
    (void)enable_srgb_storage;(void)queries;
    source_tex.pub=(VkTexImage){.image=(VkImage)(uintptr_t)99,.format=VK_FORMAT_R8G8B8A8_UNORM,.type=VK_IMAGE_TYPE_2D,
        .view_type=VK_IMAGE_VIEW_TYPE_CUBE_ARRAY,.width=1,.height=1,.depth=1,.levels=1,.layers=6,.samples=1,
        .mirrored=1,.aspect=VK_IMAGE_ASPECT_COLOR_BIT,.swz={2,3,4,5}};
    source_tex.t.d=(nvn_texdesc){.target=NVN_TEXTURE_TARGET_CUBEMAP_ARRAY,.format=NVN_FORMAT_RGBA8,
        .width=1,.height=1,.depth=6,.levels=1,.swizzle_r=2,.swizzle_g=3,.swizzle_b=4,.swizzle_a=5};
    int eight=!strncmp(mode,"eight",5);
    if(!strcmp(mode,"one") || eight) {
        source_tex.pub.layers=1;source_tex.pub.view_type=VK_IMAGE_VIEW_TYPE_2D_ARRAY;
        source_tex.t.d.target=NVN_TEXTURE_TARGET_2D_ARRAY;source_tex.t.d.depth=1;source_view.nlayers=1;
    }
    if(eight) {
        source_tex.pub.width=source_tex.pub.height=source_tex.t.d.width=source_tex.t.d.height=8;
        if(!strcmp(mode,"eight-cube")) {
            source_tex.pub.layers=6;source_tex.pub.view_type=VK_IMAGE_VIEW_TYPE_CUBE_ARRAY;
            source_tex.t.d.target=NVN_TEXTURE_TARGET_CUBEMAP_ARRAY;source_tex.t.d.depth=6;source_view.nlayers=6;
        }
    }
    unsigned area=source_tex.pub.width*source_tex.pub.height;
    unsigned count=area*source_tex.pub.layers;
    nvn_tex_layout(&source_tex.t.d,&source_tex.t.L);storage_bytes=(size_t)source_tex.t.L.size;
    uint8_t *allocation=calloc(1,storage_bytes+128);CHECK(allocation);storage_base=allocation+64;
    source_tex.t.mem=storage_base;source_tex.pub.addr=(uintptr_t)storage_base;
    CHECK(tex_vk_format(NVN_FORMAT_RGBA8,&source_tex.f));
    CHECK(source_tex.f.vk==VK_FORMAT_R8G8B8A8_UNORM && source_tex.f.vk==source_tex.f.ideal);
    CHECK(!source_tex.f.chan[0] && !source_tex.f.alpha_one);
    memcpy(source_tex.pub.chan,source_tex.f.chan,4);source_tex.pub.alpha_one=source_tex.f.alpha_one;
    uint8_t values[6]={0,1,127,128,254,255};
    if(!strcmp(mode,"bgr-white"))memset(values,255,sizeof values);
    for(unsigned i=0;i<source_tex.pub.layers;i++)pixel(i,values[i]);
    uint8_t readback[NVN_RGBA8_COMPARE_MAX_TEXELS][4];
    CHECK(vk_texture_compare_rgba8_pixels_locked(&source_tex.pub,readback,NVN_RGBA8_COMPARE_MAX_TEXELS));
    for(unsigned layer=0;layer<source_tex.pub.layers;layer++)for(unsigned i=0;i<area;i++)for(int j=0;j<4;j++)
        CHECK(readback[layer*area+i][j]==(uint8_t)(values[layer]+i*37));
    // A short destination is rejected atomically, even for a valid 8x8 source.
    memset(readback,0xa5,sizeof readback);
    CHECK(!vk_texture_compare_rgba8_pixels_locked(&source_tex.pub,readback,count-1));
    for(unsigned i=0;i<sizeof readback;i++)CHECK(((uint8_t *)readback)[i]==0xa5);
    // Exhaustive scalar UNORM equality, including endpoints and intermediate values; failure is atomic.
    for(unsigned value=0;value<256;value++) {
        uint8_t data[6][4]={{0}};uint16_t d[6];memset(data[0],(int)value,4);
        CHECK(nvn_rgba8_compare_depth(data,1,d) && d[0]==value*257 && (uint64_t)d[0]*255==value*65535u);
    }
    uint16_t untouched[6]={1,2,3,4,5,6},expected[6];memcpy(expected,untouched,sizeof expected);
    uint8_t unequal[6][4]={{1,2,1,1}};CHECK(!nvn_rgba8_compare_depth(unequal,1,untouched));
    CHECK(!memcmp(expected,untouched,sizeof expected));
    VkExec ex={0};GfxShaderRes r={.type=source_tex.pub.layers==6 ? 0x505 : 0x503};
    if(!strcmp(mode,"unequal"))storage_base[1]=1;
    else if(!strcmp(mode,"gpu-owned"))source_tex.pub.gpu_owned=1;
    else if(!strcmp(mode,"gpu-write"))source_tex.wr_fence=1;
    else if(!strcmp(mode,"released"))source_tex.released=1;
    else if(!strcmp(mode,"not-mirrored"))source_tex.pub.mirrored=0;
    else if(!strcmp(mode,"unreadable"))readable=0;
    else if(!strcmp(mode,"width"))source_tex.pub.width=9;
    else if(!strcmp(mode,"mips"))source_tex.pub.levels=2;
    else if(!strcmp(mode,"layers"))source_tex.pub.layers=2;
    else if(!strcmp(mode,"storage-shape"))source_tex.t.d.width=2;
    else if(!strcmp(mode,"storage-format"))source_tex.t.d.format=NVN_FORMAT_DEPTH16;
    else if(!strcmp(mode,"storage-target"))source_tex.t.d.target=NVN_TEXTURE_TARGET_3D;
    else if(!strcmp(mode,"missing-storage"))source_tex.t.mem=NULL;
    else if(!strcmp(mode,"swizzle"))source_tex.pub.swz[0]=NVN_TEXTURE_SWIZZLE_ONE;
    else if(!strcmp(mode,"format-identity"))memcpy(source_tex.pub.chan,(uint8_t[]){2,3,4,5},4);
    else if(!strcmp(mode,"sentinel"))memcpy(source_tex.pub.chan,(uint8_t[]){0,9,8,7},4);
    else if(!strcmp(mode,"inverse-map")) {
        memcpy(source_tex.pub.chan,(uint8_t[]){4,3,2,5},4);memcpy(source_tex.pub.swz,(uint8_t[]){4,3,2,5},4);
    }
    else if(!strcmp(mode,"view-identity")) {
        source_tex.pub.swz[0]=NVN_TEXTURE_SWIZZLE_ONE;source_view.set_swizzle=1;
        memcpy(source_view.swz,(uint8_t[]){2,3,4,5},4);
    }
    else if(!strcmp(mode,"channel-map"))memcpy(source_tex.pub.chan,(uint8_t[]){3,2,4,5},4);
    else if(!strncmp(mode,"bgr-",4)) {
        source_view.set_swizzle=1;memcpy(source_view.swz,(uint8_t[]){4,3,2,5},4);
        if(!strcmp(mode,"bgr-unequal"))storage_base[1]=1;
        else if(!strcmp(mode,"bgr-missing-sampler"))sampler_present=0;
        else if(!strcmp(mode,"bgr-no-compare"))source_sampler.cmp_mode=0;
        else if(!strcmp(mode,"bgr-border"))source_sampler.ws=NVN_WRAP_MODE_CLAMP_TO_BORDER;
        else if(!strcmp(mode,"bgr-mixed-border")) {
            source_sampler.wt=NVN_WRAP_MODE_MIRROR_CLAMP_TO_BORDER;
            source_sampler.border[0]=0.25f;source_sampler.border[1]=0.5f;source_sampler.border[2]=0.75f;
        }
        else if(!strcmp(mode,"bgr-invalid-wrap"))source_sampler.wr=8;
        else CHECK(!strcmp(mode,"bgr-values") || !strcmp(mode,"bgr-white"));
    }
    else if(!strcmp(mode,"constant-view")) {
        source_view.set_swizzle=1;memcpy(source_view.swz,(uint8_t[]){2,3,4,1},4);
    }
    else if(!strcmp(mode,"alpha-one"))source_tex.pub.alpha_one=1;
    else if(!strcmp(mode,"mip-range"))source_view.base_level=1;
    else if(!strcmp(mode,"layer-range"))source_view.min_layer=1;
    else if(!strcmp(mode,"feature"))depth_feature=0;
    else if(!strcmp(mode,"linear-feature"))linear_feature=0;
    else if(!strcmp(mode,"shape"))shape_supported=0;
    else if(!strcmp(mode,"create"))allocation_ok=0;
    else if(!strcmp(mode,"limit"))rgba8_compare_count=64;
    else if(!strcmp(mode,"eight-unequal-tail")) {
        uint8_t linear[64][4];CHECK(!nvn_deswizzle(&source_tex.t.d,0,0,storage_base,linear,32));
        linear[63][3]^=1;CHECK(!nvn_swizzle(&source_tex.t.d,0,0,linear,32,storage_base));
    }
    else CHECK(!strcmp(mode,"one") || !strcmp(mode,"six") || eight);
    // A rejected source never partially replaces the caller's pixels. The
    // eventual tex_info fatal must retain the exact predicate in its diagnostic.
    if(!strcmp(mode,"gpu-owned") || !strcmp(mode,"gpu-write") || !strcmp(mode,"released") ||
       !strcmp(mode,"not-mirrored") || !strcmp(mode,"unreadable") || !strcmp(mode,"width") ||
       !strcmp(mode,"mips") || !strcmp(mode,"layers") || !strcmp(mode,"storage-shape") ||
       !strcmp(mode,"storage-format") || !strcmp(mode,"storage-target") || !strcmp(mode,"missing-storage")) {
        uint8_t untouched_pixels[6][4];memset(untouched_pixels,0xa5,sizeof untouched_pixels);
        CHECK(!vk_texture_compare_rgba8_pixels_locked(&source_tex.pub,untouched_pixels,6));
        for(unsigned i=0;i<sizeof untouched_pixels;i++)CHECK(((uint8_t *)untouched_pixels)[i]==0xa5);
    }
    VkDescriptorImageInfo out;tex_info(&ex,&r,5,0,0,&out);
    CHECK(allocations==1 && uploads==1 && !upload_step && out.imageLayout==VK_IMAGE_LAYOUT_GENERAL);
    CHECK(out.sampler==(VkSampler)(uintptr_t)123 && created.format==VK_FORMAT_D16_UNORM && created_usage==VK_IMAGE_USAGE_SAMPLED_BIT);
    CHECK(source_view.set_format==1 && source_view.format==NVN_FORMAT_RGBA8);
    CHECK(created.components.r==VK_COMPONENT_SWIZZLE_R && created.components.g==VK_COMPONENT_SWIZZLE_G &&
          created.components.b==VK_COMPONENT_SWIZZLE_B && created.components.a==VK_COMPONENT_SWIZZLE_A);
    CHECK(allocated.arrayLayers==source_tex.pub.layers && allocated.flags==(source_tex.pub.layers==6 ? VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT:0));
    for(unsigned layer=0;layer<source_tex.pub.layers;layer++)for(unsigned i=0;i<area;i++)
        CHECK(copied[layer*area+i]==(uint8_t)(values[layer]+i*37)*257u);
    VkImageView first=out.imageView;tex_info(&ex,&r,5,0,0,&out);CHECK(out.imageView==first && uploads==1);
    // Modified source creates a new immutable version; the first content and view survive for already recorded draws.
    pixel(0,42);tex_info(&ex,&r,5,0,0,&out);CHECK(out.imageView!=first && uploads==2 && copied[0]==42*257);
    CHECK(rgba8_compares->next->rgba[0][0]==values[0]);
    pixel(0,values[0]);tex_info(&ex,&r,5,0,0,&out);CHECK(out.imageView==first && uploads==2);
    // A layer-subset array view of the six-face data keeps the actual base/count and does not reupload.
    if(source_tex.pub.layers==6) {
        r.type=0x503;source_view.min_layer=2;source_view.nlayers=3;
        tex_info(&ex,&r,5,0,0,&out);CHECK(uploads==2 && created.subresourceRange.baseArrayLayer==2 &&
            created.subresourceRange.layerCount==3 && created.subresourceRange.baseMipLevel==0);
    }
    if(eight) {
        // Change only the last texel: first-pixel-only caches must not reuse the
        // old image. Reverting the source must find the immutable original.
        uint8_t linear[64][4];CHECK(!nvn_deswizzle(&source_tex.t.d,0,0,storage_base,linear,32));
        memset(linear[63],123,4);CHECK(!nvn_swizzle(&source_tex.t.d,0,0,linear,32,storage_base));
        tex_info(&ex,&r,5,0,0,&out);CHECK(uploads==3 && out.imageView!=first && copied[63]==123*257u);
        pixel(0,values[0]);tex_info(&ex,&r,5,0,0,&out);CHECK(uploads==3);
    }
    unsigned expected_images=eight?3:2;
    if(eight && source_tex.pub.layers==1) {
        source_tex.pub.width=source_tex.t.d.width=4;source_tex.pub.height=source_tex.t.d.height=8;
        nvn_tex_layout(&source_tex.t.d,&source_tex.t.L);CHECK(source_tex.t.L.size==storage_bytes);pixel(0,values[0]);
        tex_info(&ex,&r,5,0,0,&out);VkImageView portrait=out.imageView;CHECK(uploads==4 && allocated.extent.width==4);
        source_tex.pub.width=source_tex.t.d.width=8;source_tex.pub.height=source_tex.t.d.height=4;
        nvn_tex_layout(&source_tex.t.d,&source_tex.t.L);CHECK(source_tex.t.L.size==storage_bytes);pixel(0,values[0]);
        tex_info(&ex,&r,5,0,0,&out);CHECK(uploads==5 && allocated.extent.width==8 && out.imageView!=portrait);
        expected_images=5;
    }
    rgba8_compare_release(&context);CHECK(destroyed_images==expected_images && destroyed_views==creates && !rgba8_compares);
    CHECK(!view_of.n && !views_by_image.n);free(allocation);
    puts("RGBA8 comparison check passed");return 0;
}
'''
    return (base + types + stubs + function(tex,"int vk_texture_compare_rgba8_pixels_locked(") + conversion +
            function(draw,"static void tex_info(") + main)


@unittest.skipUnless(os.name == "nt" and CC.is_file() and DEPS.is_dir(), "Windows Clang and Vulkan headers required")
class Rgba8CompareTests(unittest.TestCase):
    def test_sampler_snapshot_lock_contract(self):
        tex = (ROOT / "runtime/vk_texture.c").read_text()
        fixture = r'''
#include <string.h>
#include <stdlib.h>
#include "vk.h"
#define CHECK(x) do { if(!(x))exit(1); } while(0)
enum {DESC_SAMPLER=2};
typedef struct {int kind;struct {struct {samp_par p;} sampler;} u;} DescSlot;
static DescSlot slot={.kind=DESC_SAMPLER,.u.sampler.p={.cmp_mode=1,.ws=7,.wt=7,.wr=7}};
static int held, locks, unlocks, present=1;
static void lock(void) {CHECK(!held);held=1;locks++;}
static void unlock(void) {CHECK(held);held=0;unlocks++;}
static DescSlot *descriptor_slot(const dpool_ref *p,int id,unsigned limit,int create) {
    CHECK(held && p && id==277 && limit==4096 && !create);return present?&slot:NULL;
}
''' + function(tex,"int vk_sampler_params_locked(") + function(tex,"int vk_sampler_params(") + r'''
int main(void) {
    dpool_ref pool={0};samp_par out={0},old;
    held=1;CHECK(vk_sampler_params_locked(&pool,277,&out));CHECK(!locks && !unlocks && held);
    CHECK(!memcmp(&out,&slot.u.sampler.p,sizeof out));old=out;
    present=0;CHECK(!vk_sampler_params_locked(&pool,277,&out) && !memcmp(&old,&out,sizeof out));
    present=1;slot.kind=1;CHECK(!vk_sampler_params_locked(&pool,277,&out) && !memcmp(&old,&out,sizeof out));
    slot.kind=DESC_SAMPLER;held=0;CHECK(vk_sampler_params(&pool,277,&out));
    CHECK(!held && locks==1 && unlocks==1);
    present=0;CHECK(!vk_sampler_params(&pool,277,&out));CHECK(!held && locks==2 && unlocks==2);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="hwaoi-sampler-lock-") as tmp:
            path=Path(tmp)/"fixture.c";path.write_text(fixture,encoding="utf-8")
            for opt in ("-O0","-O2"):
                exe=path.with_suffix(".exe")
                result=subprocess.run([str(CC),opt,"-Wall","-Wextra","-Werror","-I",str(ROOT/"runtime"),
                    "-I",str(DEPS),"-I",str(DEPS/"vma"),str(path),"-o",str(exe)],capture_output=True,text=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                result=subprocess.run([str(exe)],capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_real_layout_conversion_versions_bindings_and_failures(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-rgba8-compare-") as tmp:
            path=Path(tmp)/"fixture.c";path.write_text(source(),encoding="utf-8")
            for opt in ("-O0","-O2"):
                exe=path.with_name(f"fixture{opt}.exe")
                result=subprocess.run([str(CC),opt,"-Wall","-Wextra","-Werror","-Wno-missing-field-initializers",
                    "-march=x86-64-v3","-I",str(ROOT/"runtime"),"-I",str(DEPS),"-I",str(DEPS/"vma"),str(path),
                    str(ROOT/"runtime/vk_formats.c"),str(ROOT/"runtime/nvn_tex.c"),str(ROOT/"runtime/tex_deswizzle.c"),
                    "-o",str(exe)],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=60)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                for case in ("one","six","eight","eight-cube","eight-unequal-tail","unequal","gpu-owned","gpu-write","released","not-mirrored","unreadable","width",
                             "mips","layers","storage-shape","storage-format","storage-target","missing-storage",
                             "swizzle","format-identity","sentinel","inverse-map","view-identity",
                             "channel-map","constant-view","alpha-one","mip-range","layer-range","feature","linear-feature",
                             "shape","create","limit","bgr-values","bgr-white","bgr-unequal","bgr-missing-sampler",
                             "bgr-no-compare","bgr-border","bgr-mixed-border","bgr-invalid-wrap"):
                    with self.subTest(opt=opt,case=case):
                        result=subprocess.run([str(exe),case],capture_output=True,text=True,timeout=10)
                        good=case in ("one","six","eight","eight-cube","format-identity","sentinel","inverse-map","view-identity","bgr-values","bgr-white")
                        self.assertEqual(result.returncode,0 if good else 73,result.stdout+result.stderr)
                        self.assertIn("check passed" if good else "vk:",result.stdout if good else result.stderr)
                        if case == "eight-unequal-tail":
                            self.assertIn("extent=8x8 layers=1 texel=63", result.stderr)
                        reasons={"gpu-owned":"GPU-owned", "gpu-write":"image-write-recorded", "released":"released",
                                 "not-mirrored":"not-mirrored", "unreadable":"unreadable-storage", "width":"image-extent",
                                 "mips":"image-mips", "layers":"image-layers", "storage-shape":"storage-shape",
                                 "storage-format":"storage-format", "storage-target":"storage-target", "missing-storage":"missing-storage"}
                        if case in reasons:
                            for field in (f"reason={reasons[case]}", "image_format=", "extent=", "mips=", "layers=",
                                          "write_fence=", "storage_format=", "storage_bytes="):
                                self.assertIn(field,result.stderr)
                        if case in ("swizzle","channel-map","constant-view","alpha-one"):
                            for field in ("texture=", "view_set=", "view=", "chan=", "alpha_one=", "effective=",
                                          "layers=6", "rgba_bytes_le=00000000,01010101,7f7f7f7f,80808080,fefefefe,ffffffff"):
                                self.assertIn(field,result.stderr)


if __name__ == "__main__":
    unittest.main()
