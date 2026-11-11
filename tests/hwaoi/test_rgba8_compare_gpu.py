"""Actual small RGBA8 conversion, immutable Vulkan images and comparison sampling."""
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
from tests.hwaoi.test_sampled_format_gpu import source as sampled_source

SHADER = r'''#version 450
layout(local_size_x=1) in;
layout(binding=0) uniform sampler2DShadow images[2];
layout(binding=1,std430) buffer Results { float value[]; } result;
void main() {
    uint i=gl_GlobalInvocationID.x,p=i%64u,mode=i/64u;
    vec2 xy=vec2(p%8u,p/8u);
    vec2 uv=(xy+(mode==3u?vec2(1.0):vec2(0.5)))/8.0;
    float ref=mode==0u?0.0:mode==1u?1.0:mode==4u?float(p==63u?255u:(p*37u)&255u)/255.0:0.5;
    result.value[i*2u]=textureLod(images[0],vec3(uv,ref),0.0);
    result.value[i*2u+1u]=textureLod(images[1],vec3(uv,ref),0.0);
}
'''


def source():
    draw = (ROOT / "runtime/vk_exec_draw.c").read_text()
    tex = (ROOT / "runtime/vk_texture.c").read_text()
    head = sampled_source().split("\nint main(int argc,char **argv)")[0]
    start = tex.index("typedef struct {\n    uint64_t addr;")
    types = tex[start:tex.index("// A slot keeps", start)]
    start = draw.index("typedef struct Rgba8Compare")
    conversion = draw[start:draw.index("// ---- program to shaders", start)]
    support = r'''
#include "platform/platform.h"
#include "nvn_rgba8_compare.h"
#undef vkCmdPipelineBarrier2
static Tex source_tex;
static uint8_t *source_storage;
static size_t source_bytes;
static VkQueue test_queue;
static VkCommandBuffer upload_cb;
static VkBuffer stage_buffer;
static VkDeviceMemory stage_memory;
static void *stage_mapping;
static unsigned images_made,images_freed;
const char *cfg_get(const char *key){(void)key;return NULL;}
int plat_mem_readable(uint64_t address,uint64_t bytes){return address==(uintptr_t)source_storage && bytes==source_bytes;}
void rt_log(const char *fmt,...){(void)fmt;}
typedef struct {VkDeviceMemory memory;} TestAllocation;
VkResult vmaCreateImage(VmaAllocator allocator,const VkImageCreateInfo *ci,const VmaAllocationCreateInfo *ai,
                       VkImage *image,VmaAllocation *allocation,VmaAllocationInfo *info) {
    (void)allocator;(void)ai;(void)info;VK(vkCreateImage(context.device,ci,NULL,image));
    VkMemoryRequirements req;vkGetImageMemoryRequirements(context.device,*image,&req);
    TestAllocation *a=calloc(1,sizeof *a);REQUIRE(a);a->memory=allocate(&req,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    VK(vkBindImageMemory(context.device,*image,a->memory,0));*allocation=(VmaAllocation)a;images_made++;return VK_SUCCESS;
}
void vmaDestroyImage(VmaAllocator allocator,VkImage image,VmaAllocation allocation){
    (void)allocator;TestAllocation *a=(TestAllocation *)allocation;vkDestroyImage(context.device,image,NULL);
    vkFreeMemory(context.device,a->memory,NULL);free(a);images_freed++;
}
VkCommandBuffer vk_upload_begin(void){
    VK(vkResetCommandBuffer(upload_cb,0));VkCommandBufferBeginInfo bi={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    VK(vkBeginCommandBuffer(upload_cb,&bi));return upload_cb;
}
void *vk_staging(VkDeviceSize size,VkBuffer *buffer,VmaAllocation *allocation){
    REQUIRE(size<=4096 && !allocation);*buffer=stage_buffer;return stage_mapping;
}
void vk_upload_end(void){
    VK(vkEndCommandBuffer(upload_cb));VkSubmitInfo si={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,
        .commandBufferCount=1,.pCommandBuffers=&upload_cb};
    VK(vkQueueSubmit(test_queue,1,&si,VK_NULL_HANDLE));VK(vkQueueWaitIdle(test_queue));
}
// Independent native-GOB oracle for this 8x8 RGBA image; do not use the
// production swizzler to construct the input that its reader will verify.
static size_t offset(unsigned pixel){unsigned x=(pixel%8)*4,y=pixel/8;
    return (x&15)+((y&1)<<4)+((y&2)<<4)+((x&16)<<2)+((y&4)<<5);
}
static int comparison(float ref,float sample,unsigned op){
    switch(op){case 0:return 0;case 1:return ref<sample;case 2:return ref==sample;case 3:return ref<=sample;
    case 4:return ref>sample;case 5:return ref!=sample;case 6:return ref>=sample;default:return 1;}
}
'''
    main = r'''
int main(int argc,char **argv){
    REQUIRE(argc==2);VkApplicationInfo app={.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO,.apiVersion=VK_API_VERSION_1_3};
    VkInstanceCreateInfo ici={.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,.pApplicationInfo=&app};
    VK(vkCreateInstance(&ici,NULL,&context.instance));uint32_t n=0;VK(vkEnumeratePhysicalDevices(context.instance,&n,NULL));REQUIRE(n);
    VkPhysicalDevice *devices=calloc(n,sizeof *devices);VK(vkEnumeratePhysicalDevices(context.instance,&n,devices));
    context.phys=devices[0];free(devices);vkGetPhysicalDeviceMemoryProperties(context.phys,&context.mem_props);
    vkGetPhysicalDeviceQueueFamilyProperties(context.phys,&n,NULL);VkQueueFamilyProperties *families=calloc(n,sizeof *families);
    vkGetPhysicalDeviceQueueFamilyProperties(context.phys,&n,families);uint32_t q=0;
    while(q<n && !(families[q].queueFlags&VK_QUEUE_COMPUTE_BIT))q++;REQUIRE(q<n);free(families);
    float priority=1;VkDeviceQueueCreateInfo qi={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex=q,.queueCount=1,.pQueuePriorities=&priority};
    VkPhysicalDeviceSynchronization2Features sync={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES,.synchronization2=VK_TRUE};
    VkDeviceCreateInfo dci={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,.pNext=&sync,.queueCreateInfoCount=1,.pQueueCreateInfos=&qi};
    VK(vkCreateDevice(context.phys,&dci,NULL,&context.device));vkGetDeviceQueue(context.device,q,0,&test_queue);
    VkCommandPoolCreateInfo pc={.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,
        .queueFamilyIndex=q};VkCommandPool pool;VK(vkCreateCommandPool(context.device,&pc,NULL,&pool));
    VkCommandBufferAllocateInfo ca={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,.commandPool=pool,
        .level=VK_COMMAND_BUFFER_LEVEL_PRIMARY,.commandBufferCount=1};
    VK(vkAllocateCommandBuffers(context.device,&ca,&upload_cb));VkCommandBuffer cb;VK(vkAllocateCommandBuffers(context.device,&ca,&cb));
    stage_buffer=buffer(4096,VK_BUFFER_USAGE_TRANSFER_SRC_BIT,&stage_memory,&stage_mapping);
    source_tex.pub=(VkTexImage){.format=VK_FORMAT_R8G8B8A8_UNORM,.type=VK_IMAGE_TYPE_2D,.view_type=VK_IMAGE_VIEW_TYPE_2D,
        .width=8,.height=8,.depth=1,.levels=1,.layers=1,.samples=1,.mirrored=1,.swz={2,3,4,5}};
    source_tex.t.d=(nvn_texdesc){.target=NVN_TEXTURE_TARGET_2D,.format=NVN_FORMAT_RGBA8,.width=8,.height=8,.depth=1,.levels=1};
    nvn_tex_layout(&source_tex.t.d,&source_tex.t.L);source_bytes=(size_t)source_tex.t.L.size;REQUIRE(source_bytes==512);
    source_storage=calloc(1,source_bytes);REQUIRE(source_storage);source_tex.t.mem=source_storage;
    source_tex.pub.addr=(uintptr_t)source_storage;REQUIRE(tex_vk_format(NVN_FORMAT_RGBA8,&source_tex.f));
    uint8_t pixels[2][64];
    for(unsigned i=0;i<64;i++){pixels[0][i]=i==63?255:(uint8_t)(i*37);memset(source_storage+offset(i),pixels[0][i],4);}
    memcpy(pixels[1],pixels[0],64);pixels[1][63]=239;
    VkExec ex={0};view_t view={.set_format=1,.format=NVN_FORMAT_RGBA8,.set_levels=1,.nlevels=1,.set_layers=1,.nlayers=1};
    samp_par logical={.cmp_mode=NVN_COMPARE_MODE_COMPARE_R_TO_TEXTURE,
        .ws=NVN_WRAP_MODE_CLAMP_TO_EDGE,.wt=NVN_WRAP_MODE_CLAMP_TO_EDGE,.wr=NVN_WRAP_MODE_CLAMP_TO_EDGE};
    VkImageView views[2];views[0]=rgba8_compare_view(&ex,&source_tex.pub,&view,1,3,&logical);
    memset(source_storage+offset(63),239,4);views[1]=rgba8_compare_view(&ex,&source_tex.pub,&view,1,3,&logical);
    REQUIRE(images_made==2 && views[0]!=views[1]);
    memset(source_storage+offset(63),255,4);REQUIRE(rgba8_compare_view(&ex,&source_tex.pub,&view,1,3,&logical)==views[0]);
    VkDeviceMemory output_memory;void *values;VkBuffer output=buffer(640*sizeof(float),VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,&output_memory,&values);
    VkDescriptorSetLayoutBinding bindings[2]={
        {.binding=0,.descriptorType=VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,.descriptorCount=2,.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT},
        {.binding=1,.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.descriptorCount=1,.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT}};
    VkDescriptorSetLayoutCreateInfo slci={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,.bindingCount=2,.pBindings=bindings};
    VkDescriptorSetLayout sl;VK(vkCreateDescriptorSetLayout(context.device,&slci,NULL,&sl));
    VkDescriptorPoolSize sizes[2]={{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,2},{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,1}};
    VkDescriptorPoolCreateInfo dpci={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,.maxSets=1,.poolSizeCount=2,.pPoolSizes=sizes};
    VkDescriptorPool dp;VK(vkCreateDescriptorPool(context.device,&dpci,NULL,&dp));
    VkDescriptorSetAllocateInfo dsai={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,.descriptorPool=dp,.descriptorSetCount=1,.pSetLayouts=&sl};
    VkDescriptorSet ds;VK(vkAllocateDescriptorSets(context.device,&dsai,&ds));
    VkPipelineLayoutCreateInfo plci={.sType=VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,.setLayoutCount=1,.pSetLayouts=&sl};
    VkPipelineLayout layout;VK(vkCreatePipelineLayout(context.device,&plci,NULL,&layout));
    FILE *f=fopen(argv[1],"rb");REQUIRE(f);fseek(f,0,SEEK_END);long bytes=ftell(f);rewind(f);
    uint32_t *code=malloc((size_t)bytes);REQUIRE(fread(code,1,(size_t)bytes,f)==(size_t)bytes);fclose(f);
    VkShaderModuleCreateInfo smci={.sType=VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,.codeSize=(size_t)bytes,.pCode=code};
    VkShaderModule module;VK(vkCreateShaderModule(context.device,&smci,NULL,&module));free(code);
    VkComputePipelineCreateInfo pci={.sType=VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,.layout=layout,
        .stage={.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,.stage=VK_SHADER_STAGE_COMPUTE_BIT,.module=module,.pName="main"}};
    VkPipeline pipeline;VK(vkCreateComputePipelines(context.device,VK_NULL_HANDLE,1,&pci,NULL,&pipeline));unsigned checks=0;
    for(unsigned filter=0;filter<2;filter++)for(unsigned op=0;op<8;op++){
        VkSamplerCreateInfo sci={.sType=VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,.magFilter=filter?VK_FILTER_LINEAR:VK_FILTER_NEAREST,
            .minFilter=filter?VK_FILTER_LINEAR:VK_FILTER_NEAREST,.mipmapMode=VK_SAMPLER_MIPMAP_MODE_NEAREST,
            .addressModeU=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,.addressModeV=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
            .addressModeW=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,.compareEnable=VK_TRUE,.compareOp=(VkCompareOp)op};
        VkSampler sampler;VK(vkCreateSampler(context.device,&sci,NULL,&sampler));
        VkDescriptorImageInfo images[2]={{sampler,views[0],VK_IMAGE_LAYOUT_GENERAL},{sampler,views[1],VK_IMAGE_LAYOUT_GENERAL}};
        VkDescriptorBufferInfo ob={output,0,640*sizeof(float)};
        VkWriteDescriptorSet writes[2]={
            {.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,.dstSet=ds,.dstBinding=0,.descriptorCount=2,
             .descriptorType=VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,.pImageInfo=images},
            {.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,.dstSet=ds,.dstBinding=1,.descriptorCount=1,
             .descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.pBufferInfo=&ob}};
        vkUpdateDescriptorSets(context.device,2,writes,0,NULL);VK(vkResetCommandBuffer(cb,0));
        VkCommandBufferBeginInfo bi={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};VK(vkBeginCommandBuffer(cb,&bi));
        vkCmdBindPipeline(cb,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline);
        vkCmdBindDescriptorSets(cb,VK_PIPELINE_BIND_POINT_COMPUTE,layout,0,1,&ds,0,NULL);vkCmdDispatch(cb,320,1,1);
        VkMemoryBarrier mb={.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER,.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT,.dstAccessMask=VK_ACCESS_HOST_READ_BIT};
        vkCmdPipelineBarrier(cb,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,1,&mb,0,NULL,0,NULL);
        VK(vkEndCommandBuffer(cb));VkSubmitInfo si={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&cb};
        VK(vkQueueSubmit(test_queue,1,&si,VK_NULL_HANDLE));VK(vkQueueWaitIdle(test_queue));
        for(unsigned i=0;i<320;i++)for(unsigned version=0;version<2;version++){
            unsigned p=i%64,mode=i/64,x=p%8,y=p/8;
            float ref=mode==0?0.0f:mode==1?1.0f:mode==4?pixels[0][p]/255.0f:0.5f;
            float expected=0;
            if(mode==3 && filter){
                for(unsigned dy=0;dy<2;dy++)for(unsigned dx=0;dx<2;dx++){
                    unsigned xx=x+dx<8?x+dx:7,yy=y+dy<8?y+dy:7;
                    expected+=comparison(ref,pixels[version][yy*8+xx]/255.0f,op)*0.25f;
                }
            }else{
                if(mode==3){x=x+1<8?x+1:7;y=y+1<8?y+1:7;}
                expected=(float)comparison(ref,pixels[version][y*8+x]/255.0f,op);
            }
            float actual=((float *)values)[i*2+version];checks++;
            if(!isfinite(actual)||fabsf(expected-actual)>0.0001f){
                fprintf(stderr,"filter %u op %u index %u version %u expected %g actual %g\n",filter,op,i,version,expected,actual);return 1;
            }
        }
        vkDestroySampler(context.device,sampler,NULL);
    }
    rgba8_compare_release(&context);REQUIRE(images_made==images_freed && images_made==2);
    vkDestroyPipeline(context.device,pipeline,NULL);vkDestroyShaderModule(context.device,module,NULL);
    vkDestroyPipelineLayout(context.device,layout,NULL);vkDestroyDescriptorPool(context.device,dp,NULL);
    vkDestroyDescriptorSetLayout(context.device,sl,NULL);vkDestroyCommandPool(context.device,pool,NULL);
    vkUnmapMemory(context.device,stage_memory);vkUnmapMemory(context.device,output_memory);
    vkDestroyBuffer(context.device,stage_buffer,NULL);vkDestroyBuffer(context.device,output,NULL);
    vkFreeMemory(context.device,stage_memory,NULL);vkFreeMemory(context.device,output_memory,NULL);
    vkDestroyDevice(context.device,NULL);vkDestroyInstance(context.instance,NULL);free(source_storage);
    printf("small RGBA8 GPU comparison: %u checks passed\n",checks);return 0;
}
'''
    return (head + types + support + function(tex, "int vk_texture_compare_rgba8_pixels_locked(") + conversion + main)


@unittest.skipUnless(os.name == "nt" and CC.is_file() and GLSL.is_file() and DEPS.is_dir(), "local Vulkan GPU and tools required")
class SmallCompareGpuTests(unittest.TestCase):
    def test_nonuniform_nearest_pcf_all_operators_and_immutable_versions(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-small-compare-gpu-") as tmp:
            folder = Path(tmp)
            (folder / "fixture.c").write_text(source(), encoding="utf-8")
            (folder / "compare.comp").write_text(SHADER, encoding="utf-8")
            commands = ([str(GLSL), "-V", "compare.comp", "-o", "compare.spv"],
                        [str(CC), "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers",
                         "-march=x86-64-v3", "-I", str(ROOT / "runtime"), "-I", str(DEPS / "include"), "-I", str(DEPS / "include/vma"),
                         "fixture.c", str(ROOT / "runtime/vk_formats.c"), str(ROOT / "runtime/nvn_tex.c"),
                         str(ROOT / "runtime/tex_deswizzle.c"), "-L", str(DEPS / "lib"), "-lvulkan-1", "-o", "fixture.exe"])
            for command in commands:
                result = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(folder / "fixture.exe"), "compare.spv"], cwd=folder,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("10240 checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
