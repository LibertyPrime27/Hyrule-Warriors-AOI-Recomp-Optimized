"""Real Vulkan execution of the private UBO clone, with original synthetic bytes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
DEPS = Path("F:/TotKDependencies/runtime/deps")
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function


def source():
    draw = (ROOT / "runtime/vk_exec_draw.c").read_text(encoding="utf-8")
    return r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include "vk_exec.h"
#undef vkCmdCopyBuffer
#undef vkCmdPipelineBarrier2
#undef vkCmdUpdateBuffer
#define CHECK(x) do{if(!(x)){fprintf(stderr,"check %d: %s\n",__LINE__,#x);exit(1);}}while(0)
#define VK(x) CHECK((x)==VK_SUCCESS)
enum {BYTES=65536+1024,OFF=256};
static VkPhysicalDeviceMemoryProperties memory;
static VkDevice device;
static VkBuffer destination;
static unsigned char *destination_map;
void rt_fatal(const char *fmt,...){va_list a;va_start(a,fmt);vfprintf(stderr,fmt,a);va_end(a);exit(73);}
static uint8_t *scratch(VkExec *ex,uint64_t size,VkBuffer *buffer,VkDeviceSize *offset){
 CHECK(ex && size<=65536);*buffer=destination;*offset=OFF;return destination_map+OFF;
}
static VkBuffer make_buffer(VkDeviceMemory *allocation,unsigned char **mapped){
 VkBufferCreateInfo b={.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,.size=BYTES,
  .usage=VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT};
 VkBuffer result;VK(vkCreateBuffer(device,&b,NULL,&result));VkMemoryRequirements r;
 vkGetBufferMemoryRequirements(device,result,&r);unsigned type=0;
 while(type<memory.memoryTypeCount && (!(r.memoryTypeBits&(1u<<type)) ||
  (memory.memoryTypes[type].propertyFlags&(VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))!=
  (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)))type++;
 CHECK(type<memory.memoryTypeCount);VkMemoryAllocateInfo a={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
  .allocationSize=r.size,.memoryTypeIndex=type};VK(vkAllocateMemory(device,&a,NULL,allocation));
 VK(vkBindBufferMemory(device,result,*allocation,0));VK(vkMapMemory(device,*allocation,0,BYTES,0,(void**)mapped));return result;
}
''' + function(draw, "static VkDescriptorBufferInfo hwaoi_scene_sharpness_copy(") + r'''
int main(void){
 VkApplicationInfo a={.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO,.apiVersion=VK_API_VERSION_1_3};
 VkInstanceCreateInfo i={.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,.pApplicationInfo=&a};VkInstance instance;
 VK(vkCreateInstance(&i,NULL,&instance));unsigned n=0;VK(vkEnumeratePhysicalDevices(instance,&n,NULL));CHECK(n);
 VkPhysicalDevice *all=calloc(n,sizeof*all);VK(vkEnumeratePhysicalDevices(instance,&n,all));VkPhysicalDevice physical=all[0];free(all);
 vkGetPhysicalDeviceMemoryProperties(physical,&memory);vkGetPhysicalDeviceQueueFamilyProperties(physical,&n,NULL);
 VkQueueFamilyProperties *families=calloc(n,sizeof*families);vkGetPhysicalDeviceQueueFamilyProperties(physical,&n,families);
 unsigned family=0;while(family<n && !(families[family].queueFlags&VK_QUEUE_COMPUTE_BIT))family++;CHECK(family<n);free(families);
 float priority=1;VkDeviceQueueCreateInfo q={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,.queueFamilyIndex=family,
  .queueCount=1,.pQueuePriorities=&priority};
 VkPhysicalDeviceSynchronization2Features s={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES,.synchronization2=VK_TRUE};
 VkDeviceCreateInfo d={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,.pNext=&s,.queueCreateInfoCount=1,.pQueueCreateInfos=&q};
 VK(vkCreateDevice(physical,&d,NULL,&device));VkQueue queue;vkGetDeviceQueue(device,family,0,&queue);
 VkDeviceMemory sm,dm;unsigned char *src;VkBuffer input=make_buffer(&sm,&src);destination=make_buffer(&dm,&destination_map);
 VkCommandPoolCreateInfo pc={.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,
  .queueFamilyIndex=family};VkCommandPool pool;VK(vkCreateCommandPool(device,&pc,NULL,&pool));
 VkCommandBufferAllocateInfo ca={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,.commandPool=pool,
  .level=VK_COMMAND_BUFFER_LEVEL_PRIMARY,.commandBufferCount=1};VkExec ex={0};VK(vkAllocateCommandBuffers(device,&ca,&ex.cb));
 const unsigned ranges[]={80,256,65536};const uint32_t params[]={0x3ecccccd,0x36663666};unsigned checked=0;
 for(unsigned test=0;test<3;test++){
  for(unsigned k=0;k<BYTES;k++)src[k]=(unsigned char)(k*31+7);memset(destination_map,0xe1,BYTES);
  VK(vkResetCommandBuffer(ex.cb,0));VkCommandBufferBeginInfo begin={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
  VK(vkBeginCommandBuffer(ex.cb,&begin));const uint32_t gpu_value=0xabcde123;
  vkCmdUpdateBuffer(ex.cb,input,OFF+64,4,&gpu_value);
  VkDescriptorBufferInfo original={input,OFF,ranges[test]};VkDescriptorBufferInfo copy=hwaoi_scene_sharpness_copy(&ex,&original,params);
  CHECK(copy.buffer==destination&&copy.offset==OFF&&copy.range==ranges[test]);
  VkMemoryBarrier2 host={.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER_2,.srcStageMask=VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
   .srcAccessMask=VK_ACCESS_2_MEMORY_WRITE_BIT,.dstStageMask=VK_PIPELINE_STAGE_2_HOST_BIT,.dstAccessMask=VK_ACCESS_2_HOST_READ_BIT};
  VkDependencyInfo dep={.sType=VK_STRUCTURE_TYPE_DEPENDENCY_INFO,.memoryBarrierCount=1,.pMemoryBarriers=&host};
  vkCmdPipelineBarrier2(ex.cb,&dep);VK(vkEndCommandBuffer(ex.cb));
  VkSubmitInfo submit={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&ex.cb};
  VK(vkQueueSubmit(queue,1,&submit,VK_NULL_HANDLE));VK(vkQueueWaitIdle(queue));
  for(unsigned k=0;k<BYTES;k++){
   unsigned char expected=(unsigned char)(k*31+7);if(k>=OFF+64&&k<OFF+68)expected=((const unsigned char*)&gpu_value)[k-OFF-64];
   CHECK(src[k]==expected);
   unsigned char result=0xe1;if(k>=OFF&&k<OFF+ranges[test])result=k<OFF+8?((const unsigned char*)params)[k-OFF]:expected;
   CHECK(destination_map[k]==result);checked+=2;
  }
 }
 vkDestroyCommandPool(device,pool,NULL);vkUnmapMemory(device,sm);vkUnmapMemory(device,dm);
 vkDestroyBuffer(device,input,NULL);vkDestroyBuffer(device,destination,NULL);vkFreeMemory(device,sm,NULL);vkFreeMemory(device,dm,NULL);
 vkDestroyDevice(device,NULL);vkDestroyInstance(instance,NULL);printf("scene sharpness GPU: %u byte checks\n",checked);return 0;
}
'''


@unittest.skipUnless(os.name == "nt" and CC.is_file() and DEPS.is_dir(), "local Vulkan toolchain required")
class SceneSharpnessGpuTests(unittest.TestCase):
    def test_gpu_written_source_tail_and_guards_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-scene-copy-gpu-") as folder:
            folder = Path(folder)
            path = folder / "fixture.c"
            path.write_text(source(), encoding="utf-8")
            built = subprocess.run([str(CC), "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers",
                "-I", str(ROOT / "runtime"), "-I", str(DEPS / "include"), "-I", str(DEPS / "include/vma"),
                str(path), "-L", str(DEPS / "lib"), "-lvulkan-1", "-o", str(folder / "fixture.exe")],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            run = subprocess.run([str(folder / "fixture.exe")], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("399360 byte checks", run.stdout)


if __name__ == "__main__":
    unittest.main()
