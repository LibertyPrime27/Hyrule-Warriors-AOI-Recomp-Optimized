"""Original texels sampled through the actual view policy on a local Vulkan GPU."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
GLSL = Path("F:/GCC/ucrt64/bin/glslangValidator.exe")
DEPS = Path("F:/TotKDependencies/runtime/deps")
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function

SHADER = """#version 450
layout(local_size_x=1) in;
layout(binding=0) uniform sampler2D sourceImages[2];
layout(binding=1,std430) buffer Results { vec4 value[2]; } result;
void main() {
    result.value[0]=textureLod(sourceImages[0],vec2(0.5),0.0);
    result.value[1]=textureLod(sourceImages[1],vec2(0.5),0.0);
}
"""


def source():
    draw = (ROOT / "runtime/vk_exec_draw.c").read_text()
    start = draw.index("typedef struct {\n    VkImage image;\n    VkFormat format;")
    key = draw[start:draw.index("typedef struct { uint64_t addr, size; } Range;", start)]
    start = draw.index("typedef struct ViewNode")
    nodes = draw[start:draw.index("// An image view of a registered texture", start)]
    head = r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <math.h>
#include "tex.h"
#include "vk_exec.h"
#include "nvn_hmap.h"
// The standalone fixture records directly, without the runtime command cache.
#undef vkCmdCopyBufferToImage
#undef vkCmdBindPipeline
#undef vkCmdBindDescriptorSets
#undef vkCmdDispatch
#define REQUIRE(x) do {if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
#define VK(x) REQUIRE((x)==VK_SUCCESS)
static VkCtx context;
VkCtx *vk_ctx(void) {return &context;}
void rt_fatal(const char *fmt,...) {va_list a;va_start(a,fmt);vfprintf(stderr,fmt,a);va_end(a);exit(73);}
int vk_astc_native(VkFormat f) {(void)f;return 0;}
int vk_astc_bc(void) {return 0;}
static uint32_t memory_type(uint32_t bits,VkMemoryPropertyFlags flags) {
    for(uint32_t i=0;i<context.mem_props.memoryTypeCount;i++)
        if((bits&(1u<<i)) && (context.mem_props.memoryTypes[i].propertyFlags&flags)==flags)return i;
    rt_fatal("no compatible GPU memory type");return 0;
}
static VkDeviceMemory allocate(const VkMemoryRequirements *r,VkMemoryPropertyFlags flags) {
    VkMemoryAllocateInfo a={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .allocationSize=r->size,.memoryTypeIndex=memory_type(r->memoryTypeBits,flags)};
    VkDeviceMemory m;VK(vkAllocateMemory(context.device,&a,NULL,&m));return m;
}
static VkBuffer buffer(VkDeviceSize size,VkBufferUsageFlags usage,VkDeviceMemory *memory,void **mapped) {
    VkBufferCreateInfo c={.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,.size=size,.usage=usage,
        .sharingMode=VK_SHARING_MODE_EXCLUSIVE};
    VkBuffer b;VK(vkCreateBuffer(context.device,&c,NULL,&b));VkMemoryRequirements r;
    vkGetBufferMemoryRequirements(context.device,b,&r);
    *memory=allocate(&r,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VK(vkBindBufferMemory(context.device,b,*memory,0));VK(vkMapMemory(context.device,*memory,0,size,0,mapped));return b;
}
'''
    tail = r'''
int main(int argc,char **argv) {
    REQUIRE(argc==3);int srgb_base=atoi(argv[1]);
    VkApplicationInfo ai={.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO,.apiVersion=VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici={.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,.pApplicationInfo=&ai};
    VK(vkCreateInstance(&ici,NULL,&context.instance));uint32_t n=0;
    VK(vkEnumeratePhysicalDevices(context.instance,&n,NULL));REQUIRE(n);
    VkPhysicalDevice *devices=calloc(n,sizeof *devices);VK(vkEnumeratePhysicalDevices(context.instance,&n,devices));
    context.phys=devices[0];free(devices);
    vkGetPhysicalDeviceMemoryProperties(context.phys,&context.mem_props);
    vkGetPhysicalDeviceQueueFamilyProperties(context.phys,&n,NULL);
    VkQueueFamilyProperties *families=calloc(n,sizeof *families);
    vkGetPhysicalDeviceQueueFamilyProperties(context.phys,&n,families);uint32_t q=0;
    while(q<n && !(families[q].queueFlags&VK_QUEUE_COMPUTE_BIT))q++;REQUIRE(q<n);free(families);
    float priority=1;
    VkDeviceQueueCreateInfo qi={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,.queueFamilyIndex=q,
        .queueCount=1,.pQueuePriorities=&priority};
    VkDeviceCreateInfo dci={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,.queueCreateInfoCount=1,.pQueueCreateInfos=&qi};
    VK(vkCreateDevice(context.phys,&dci,NULL,&context.device));VkQueue queue;vkGetDeviceQueue(context.device,q,0,&queue);
    VkImageCreateInfo ii={.sType=VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,.flags=VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT,
        .imageType=VK_IMAGE_TYPE_2D,.format=srgb_base?VK_FORMAT_R8G8B8A8_SRGB:VK_FORMAT_R8G8B8A8_UNORM,
        .extent={1,1,1},.mipLevels=1,.arrayLayers=1,.samples=VK_SAMPLE_COUNT_1_BIT,.tiling=VK_IMAGE_TILING_OPTIMAL,
        .usage=VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT,.sharingMode=VK_SHARING_MODE_EXCLUSIVE};
    VkImage image;VK(vkCreateImage(context.device,&ii,NULL,&image));VkMemoryRequirements req;
    vkGetImageMemoryRequirements(context.device,image,&req);VkDeviceMemory im=allocate(&req,0);
    VK(vkBindImageMemory(context.device,image,im,0));
    VkTexImage tex={.image=image,.format=ii.format,.type=ii.imageType,.view_type=VK_IMAGE_VIEW_TYPE_2D,
        .aspect=VK_IMAGE_ASPECT_COLOR_BIT,.usage=ii.usage,.create_flags=ii.flags,
        .width=1,.height=1,.depth=1,.levels=1,.layers=1,.samples=1,.swz={2,3,4,5}};
    view_t linear={.set_format=1,.format=NVN_FORMAT_RGBA8};
    view_t srgb={.set_format=1,.format=NVN_FORMAT_RGBA8_SRGB};
    VkImageView views[2]={get_view(NULL,&tex,&linear,1,3,0),get_view(NULL,&tex,&srgb,1,3,0)};
    REQUIRE(views[0] && views[1] && views[0]!=views[1]);
    VkSamplerCreateInfo sci={.sType=VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,.magFilter=VK_FILTER_NEAREST,
        .minFilter=VK_FILTER_NEAREST,.mipmapMode=VK_SAMPLER_MIPMAP_MODE_NEAREST,
        .addressModeU=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,.addressModeV=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        .addressModeW=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE};
    VkSampler sampler;VK(vkCreateSampler(context.device,&sci,NULL,&sampler));
    VkDeviceMemory sm,om;void *pixels,*values;
    VkBuffer src=buffer(4,VK_BUFFER_USAGE_TRANSFER_SRC_BIT,&sm,&pixels);
    VkBuffer dst=buffer(32,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,&om,&values);
    VkDescriptorSetLayoutBinding bindings[2]={
        {.binding=0,.descriptorType=VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,.descriptorCount=2,.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT},
        {.binding=1,.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.descriptorCount=1,.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT}};
    VkDescriptorSetLayoutCreateInfo slci={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .bindingCount=2,.pBindings=bindings};
    VkDescriptorSetLayout sl;VK(vkCreateDescriptorSetLayout(context.device,&slci,NULL,&sl));
    VkDescriptorPoolSize sizes[2]={{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,2},{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,1}};
    VkDescriptorPoolCreateInfo dpci={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,.maxSets=1,.poolSizeCount=2,.pPoolSizes=sizes};
    VkDescriptorPool dp;VK(vkCreateDescriptorPool(context.device,&dpci,NULL,&dp));
    VkDescriptorSetAllocateInfo dsai={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,.descriptorPool=dp,.descriptorSetCount=1,.pSetLayouts=&sl};
    VkDescriptorSet ds;VK(vkAllocateDescriptorSets(context.device,&dsai,&ds));
    VkDescriptorImageInfo images[2]={{sampler,views[0],VK_IMAGE_LAYOUT_GENERAL},{sampler,views[1],VK_IMAGE_LAYOUT_GENERAL}};
    VkDescriptorBufferInfo ob={dst,0,32};
    VkWriteDescriptorSet writes[2]={
        {.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,.dstSet=ds,.dstBinding=0,.descriptorCount=2,.descriptorType=VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,.pImageInfo=images},
        {.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,.dstSet=ds,.dstBinding=1,.descriptorCount=1,.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.pBufferInfo=&ob}};
    vkUpdateDescriptorSets(context.device,2,writes,0,NULL);
    VkPipelineLayoutCreateInfo plci={.sType=VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,.setLayoutCount=1,.pSetLayouts=&sl};
    VkPipelineLayout layout;VK(vkCreatePipelineLayout(context.device,&plci,NULL,&layout));
    FILE *f=fopen(argv[2],"rb");REQUIRE(f);fseek(f,0,SEEK_END);long length=ftell(f);rewind(f);
    uint32_t *code=malloc((size_t)length);REQUIRE(fread(code,1,(size_t)length,f)==(size_t)length);fclose(f);
    VkShaderModuleCreateInfo mci={.sType=VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,.codeSize=(size_t)length,.pCode=code};
    VkShaderModule module;VK(vkCreateShaderModule(context.device,&mci,NULL,&module));free(code);
    VkComputePipelineCreateInfo pci={.sType=VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,.layout=layout,
        .stage={.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,.stage=VK_SHADER_STAGE_COMPUTE_BIT,.module=module,.pName="main"}};
    VkPipeline pipeline;VK(vkCreateComputePipelines(context.device,VK_NULL_HANDLE,1,&pci,NULL,&pipeline));
    VkCommandPoolCreateInfo cpci={.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,.queueFamilyIndex=q};
    VkCommandPool pool;VK(vkCreateCommandPool(context.device,&cpci,NULL,&pool));
    VkCommandBufferAllocateInfo cai={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,.commandPool=pool,.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY,.commandBufferCount=1};
    VkCommandBuffer cb;VK(vkAllocateCommandBuffers(context.device,&cai,&cb));
    const unsigned char tests[4][4]={{128,64,192,64},{0,0,0,128},{255,255,255,0},{10,11,12,255}};
    for(unsigned test=0;test<4;test++) {
        memcpy(pixels,tests[test],4);VK(vkResetCommandBuffer(cb,0));
        VkCommandBufferBeginInfo bi={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};VK(vkBeginCommandBuffer(cb,&bi));
        VkImageMemoryBarrier ib={.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
            .srcAccessMask=test?VK_ACCESS_SHADER_READ_BIT:0,.dstAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT,
            .oldLayout=test?VK_IMAGE_LAYOUT_GENERAL:VK_IMAGE_LAYOUT_UNDEFINED,.newLayout=VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            .srcQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,
            .image=image,.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1}};
        vkCmdPipelineBarrier(cb,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,NULL,0,NULL,1,&ib);
        VkBufferImageCopy copy={.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},.imageExtent={1,1,1}};
        vkCmdCopyBufferToImage(cb,src,image,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&copy);
        ib.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;ib.dstAccessMask=VK_ACCESS_SHADER_READ_BIT;
        ib.oldLayout=VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;ib.newLayout=VK_IMAGE_LAYOUT_GENERAL;
        vkCmdPipelineBarrier(cb,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,0,0,NULL,0,NULL,1,&ib);
        vkCmdBindPipeline(cb,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline);
        vkCmdBindDescriptorSets(cb,VK_PIPELINE_BIND_POINT_COMPUTE,layout,0,1,&ds,0,NULL);vkCmdDispatch(cb,1,1,1);
        VkMemoryBarrier mb={.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER,.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT,.dstAccessMask=VK_ACCESS_HOST_READ_BIT};
        vkCmdPipelineBarrier(cb,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,1,&mb,0,NULL,0,NULL);
        VK(vkEndCommandBuffer(cb));VkSubmitInfo submit={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&cb};
        VK(vkQueueSubmit(queue,1,&submit,VK_NULL_HANDLE));VK(vkQueueWaitIdle(queue));
        for(int view=0;view<2;view++)for(int channel=0;channel<4;channel++) {
            double value=tests[test][channel]/255.0;
            double expected=view && channel<3 ? (value<=.04045?value/12.92:pow((value+.055)/1.055,2.4)):value;
            float actual=((float *)values)[view*4+channel];
            if(!isfinite(actual)||fabs(actual-expected)>.001) {
                fprintf(stderr,"base %d test %u view %d channel %d expected %.8f actual %.8f\n",srgb_base,test,view,channel,expected,actual);return 1;
            }
        }
    }
    vkDestroyCommandPool(context.device,pool,NULL);vkDestroyPipeline(context.device,pipeline,NULL);
    vkDestroyShaderModule(context.device,module,NULL);vkDestroyPipelineLayout(context.device,layout,NULL);
    vkDestroyDescriptorPool(context.device,dp,NULL);vkDestroyDescriptorSetLayout(context.device,sl,NULL);
    vkDestroySampler(context.device,sampler,NULL);vkDestroyImageView(context.device,views[0],NULL);vkDestroyImageView(context.device,views[1],NULL);
    vkDestroyImage(context.device,image,NULL);vkFreeMemory(context.device,im,NULL);
    vkUnmapMemory(context.device,sm);vkUnmapMemory(context.device,om);
    vkDestroyBuffer(context.device,src,NULL);vkDestroyBuffer(context.device,dst,NULL);
    vkFreeMemory(context.device,sm,NULL);vkFreeMemory(context.device,om,NULL);
    vkDestroyDevice(context.device,NULL);vkDestroyInstance(context.instance,NULL);
    printf("GPU sampled format semantic check passed: base %d, 32 channel checks\n",srgb_base);return 0;
}
'''
    return (head + key + nodes + function(draw, "static VkImageViewType res_view_type(") +
            function(draw, "static VkComponentSwizzle swz_of(") + function(draw, "VkImageAspectFlags vk_exec_view_aspect(") +
            function(draw, "static int sampled_format_compatible(") + function(draw, "static VkImageView get_view(") + tail)


@unittest.skipUnless(os.name == "nt" and CC.is_file() and GLSL.is_file() and DEPS.is_dir(),
                     "local Windows Vulkan GPU/compiler/shader tools required")
class SampledFormatGpuTests(unittest.TestCase):
    def test_original_texels_keep_bytes_and_select_transfer_by_view(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-sampled-gpu-") as tmp:
            folder = Path(tmp)
            (folder / "fixture.c").write_text(source(), encoding="utf-8")
            (folder / "sample.comp").write_text(SHADER, encoding="utf-8")
            for command in ([str(GLSL), "-V", "sample.comp", "-o", "sample.spv"],
                            [str(CC), "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers",
                             "-I", str(ROOT / "runtime"), "-I", str(DEPS / "include"), "-I", str(DEPS / "include/vma"),
                             "fixture.c", str(ROOT / "runtime/vk_formats.c"), "-L", str(DEPS / "lib"),
                             "-lvulkan-1", "-o", "fixture.exe"]):
                p = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=60)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            for base in ("0", "1"):
                with self.subTest(srgb_base=base):
                    p = subprocess.run([str(folder / "fixture.exe"), base, "sample.spv"], cwd=folder,
                                       capture_output=True, text=True, timeout=30)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn("32 channel checks", p.stdout)
            # Restore the old ignored-format policy in a temporary source copy.
            # Let equal views reach the GPU so the control fails on actual texel
            # values, independently of the earlier descriptor-identity check.
            mutant = source().replace("format = requested.vk;", "format = img->format;")
            mutant = mutant.replace(" && views[0]!=views[1]", "")
            (folder / "fixture.c").write_text(mutant, encoding="utf-8")
            p = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            for base in ("0", "1"):
                p = subprocess.run([str(folder / "fixture.exe"), base, "sample.spv"], cwd=folder,
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn("expected", p.stderr)
                self.assertIn("actual", p.stderr)


if __name__ == "__main__":
    unittest.main()
