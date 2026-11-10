"""Actual GPU-written QUADS arguments, bounded conversion and immutable per-draw headers."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path("F:/llvm-mingw/bin/clang.exe")
DEPS = Path("F:/TotKDependencies/runtime/deps")


def source(recorded=False):
    module = (ROOT / "runtime/vk_quad.c").read_text(encoding="utf-8")
    direct = r'''
#undef vkCmdBindPipeline
#undef vkCmdPushConstants
#undef vkCmdDispatch
#undef vkCmdDispatchIndirect
#undef vkCmdPipelineBarrier2
#undef vkCmdCopyBuffer
#undef vkCmdUpdateBuffer
'''
    if not recorded:
        module = module.replace('#include "quad_prepare_spv.h"', '#include "quad_prepare_spv.h"\n' + direct)
    support = r'''
#include <stdio.h>
#include <stdarg.h>
#include <math.h>
#include <windows.h>
#define CHECK(x) do{if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
#define VK(x) CHECK((x)==VK_SUCCESS)
static VkCtx context;
VkCtx *vk_ctx(void){return &context;}
void rt_fatal(const char *fmt,...){va_list a;va_start(a,fmt);vfprintf(stderr,fmt,a);va_end(a);fputc('\n',stderr);fflush(stderr);TerminateProcess(GetCurrentProcess(),73);_Exit(73);}
void rt_log(const char *fmt,...){(void)fmt;}
void plat_mutex_lock(PlatMutex *m){(void)m;}
void plat_mutex_unlock(PlatMutex *m){(void)m;}
typedef struct {VkDeviceMemory memory;void *map;} Allocation;
static unsigned allocated,freed;
static uint32_t type_for(uint32_t bits,VkMemoryPropertyFlags flags){
 for(uint32_t i=0;i<context.mem_props.memoryTypeCount;i++)if((bits&(1u<<i)) &&
  (context.mem_props.memoryTypes[i].propertyFlags&flags)==flags)return i;
 rt_fatal("no memory type");return 0;
}
VkResult vmaCreateBuffer(VmaAllocator allocator,const VkBufferCreateInfo *bi,const VmaAllocationCreateInfo *ai,
 VkBuffer *b,VmaAllocation *allocation,VmaAllocationInfo *info){
 (void)allocator;VK(vkCreateBuffer(context.device,bi,NULL,b));VkMemoryRequirements r;vkGetBufferMemoryRequirements(context.device,*b,&r);
 Allocation *a=calloc(1,sizeof *a);CHECK(a);VkMemoryAllocateInfo mi={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
  .allocationSize=r.size,.memoryTypeIndex=type_for(r.memoryTypeBits,ai->requiredFlags)};
 VK(vkAllocateMemory(context.device,&mi,NULL,&a->memory));VK(vkBindBufferMemory(context.device,*b,a->memory,0));
 if(ai->flags&VMA_ALLOCATION_CREATE_MAPPED_BIT)VK(vkMapMemory(context.device,a->memory,0,bi->size,0,&a->map));
 if(info){memset(info,0,sizeof *info);info->pMappedData=a->map;}*allocation=(VmaAllocation)a;allocated++;return VK_SUCCESS;
}
void vmaDestroyBuffer(VmaAllocator allocator,VkBuffer b,VmaAllocation allocation){
 (void)allocator;Allocation *a=(Allocation *)allocation;if(a->map)vkUnmapMemory(context.device,a->memory);
 vkDestroyBuffer(context.device,b,NULL);vkFreeMemory(context.device,a->memory,NULL);free(a);freed++;
}
typedef struct{void(*fn)(void*);void *arg;} Callback;
static Callback callbacks[256];static unsigned ncallbacks;
void vk_completion_add(uint64_t f,void(*fn)(void*),void *arg){CHECK(f && ncallbacks<256);callbacks[ncallbacks++]=(Callback){fn,arg};}
void vk_exec_barrier(VkExec *ex){
 VkMemoryBarrier2 m={.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER_2,.srcStageMask=VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
  .srcAccessMask=VK_ACCESS_2_MEMORY_READ_BIT|VK_ACCESS_2_MEMORY_WRITE_BIT,.dstStageMask=VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
  .dstAccessMask=VK_ACCESS_2_MEMORY_READ_BIT|VK_ACCESS_2_MEMORY_WRITE_BIT};
 VkDependencyInfo d={.sType=VK_STRUCTURE_TYPE_DEPENDENCY_INFO,.memoryBarrierCount=1,.pMemoryBarriers=&m};vkCmdPipelineBarrier2(ex->cb,&d);
}
#ifndef QUAD_RECORDER_TEST
void rec_push(PFN_vkCmdPushDescriptorSetKHR fn,VkCommandBuffer cb,VkPipelineBindPoint bp,VkPipelineLayout l,
 uint32_t set,uint32_t n,const VkWriteDescriptorSet *w){fn(cb,bp,l,set,n,w);}
#else
struct PlatThread {HANDLE handle;PlatThreadFn fn;void *argument;};
static HANDLE consumer_gate;
unsigned rt_trace;
const char *cfg_get(const char *key){(void)key;return NULL;}
int rt_knob(const char *name,const char *def){(void)name;(void)def;return 1;}
int rt_ab(void){return 1;}
void vk_crumb(VkCommandBuffer cb,int kind,uint64_t value){(void)cb;(void)kind;(void)value;}
void plat_thread_priority(int p){(void)p;}
int plat_thread_priority_get(void){return 0;}
void plat_yield(void){SwitchToThread();}
uint64_t plat_now_ns(void){return GetTickCount64()*1000000ull;}
void plat_event_init(PlatEvent *e,int manual,int initial){memset(e,0,sizeof *e);e->m.p=CreateEvent(NULL,manual,initial,NULL);CHECK(e->m.p);}
void plat_event_set(PlatEvent *e){CHECK(SetEvent((HANDLE)e->m.p));}
int plat_event_wait_ns(PlatEvent *e,uint64_t ns){DWORD r=WaitForSingleObject((HANDLE)e->m.p,(DWORD)((ns+999999)/1000000));
 CHECK(r==WAIT_OBJECT_0 || r==WAIT_TIMEOUT);return r==WAIT_OBJECT_0;}
static DWORD WINAPI start_consumer(void *raw){PlatThread *t=raw;CHECK(WaitForSingleObject(consumer_gate,10000)==WAIT_OBJECT_0);
 t->fn(t->argument);return 0;}
PlatThread *plat_thread_new(PlatThreadFn fn,void *arg,const char *name){(void)name;CHECK(!consumer_gate);
 consumer_gate=CreateEvent(NULL,TRUE,FALSE,NULL);CHECK(consumer_gate);PlatThread *t=calloc(1,sizeof *t);CHECK(t);
 t->fn=fn;t->argument=arg;t->handle=CreateThread(NULL,0,start_consumer,t,0,NULL);CHECK(t->handle);return t;}
#endif
'''
    main = r'''
static unsigned validation_errors;
static VKAPI_ATTR VkBool32 VKAPI_CALL debug(VkDebugUtilsMessageSeverityFlagBitsEXT severity,
 VkDebugUtilsMessageTypeFlagsEXT type,const VkDebugUtilsMessengerCallbackDataEXT *data,void *user){
 (void)type;(void)user;if(severity&VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT){validation_errors++;fprintf(stderr,"validation: %s\n",data->pMessage);}return VK_FALSE;
}
static VkBuffer test_buffer(VkDeviceSize bytes,VkBufferUsageFlags usage,VmaAllocation *allocation,void **map){
 VkBufferCreateInfo bi={.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,.size=bytes,.usage=usage};
 VmaAllocationCreateInfo ai={.requiredFlags=VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
  .flags=VMA_ALLOCATION_CREATE_MAPPED_BIT};VkBuffer b;VmaAllocationInfo info;VK(vmaCreateBuffer(context.vma,&bi,&ai,&b,allocation,&info));*map=info.pMappedData;return b;
}
int main(int argc,char **argv){
 CHECK(argc==2);int invalid=!strcmp(argv[1],"bad");context.vma=(VmaAllocator)(uintptr_t)1;
 const char *layer="VK_LAYER_KHRONOS_validation",*extension=VK_EXT_DEBUG_UTILS_EXTENSION_NAME;
 VkApplicationInfo app={.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO,.apiVersion=VK_API_VERSION_1_3};
 VkDebugUtilsMessengerCreateInfoEXT debug_info={.sType=VK_STRUCTURE_TYPE_DEBUG_UTILS_MESSENGER_CREATE_INFO_EXT,
  .messageSeverity=VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT,.messageType=VK_DEBUG_UTILS_MESSAGE_TYPE_VALIDATION_BIT_EXT,
  .pfnUserCallback=debug};
 VkValidationFeatureEnableEXT sync_validation=VK_VALIDATION_FEATURE_ENABLE_SYNCHRONIZATION_VALIDATION_EXT;
 VkValidationFeaturesEXT validation={.sType=VK_STRUCTURE_TYPE_VALIDATION_FEATURES_EXT,.pNext=&debug_info,
  .enabledValidationFeatureCount=1,.pEnabledValidationFeatures=&sync_validation};
 VkInstanceCreateInfo ici={.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,.pApplicationInfo=&app,
  .enabledLayerCount=1,.ppEnabledLayerNames=&layer,.enabledExtensionCount=1,.ppEnabledExtensionNames=&extension,.pNext=&validation};
 VK(vkCreateInstance(&ici,NULL,&context.instance));
 PFN_vkCreateDebugUtilsMessengerEXT create_debug=(PFN_vkCreateDebugUtilsMessengerEXT)vkGetInstanceProcAddr(context.instance,"vkCreateDebugUtilsMessengerEXT");
 VkDebugUtilsMessengerEXT messenger;VK(create_debug(context.instance,&debug_info,NULL,&messenger));
 uint32_t n=0;VK(vkEnumeratePhysicalDevices(context.instance,&n,NULL));CHECK(n);VkPhysicalDevice *devices=calloc(n,sizeof *devices);
 VK(vkEnumeratePhysicalDevices(context.instance,&n,devices));context.phys=devices[0];free(devices);
 vkGetPhysicalDeviceMemoryProperties(context.phys,&context.mem_props);vkGetPhysicalDeviceProperties(context.phys,&context.props);
 vkGetPhysicalDeviceQueueFamilyProperties(context.phys,&n,NULL);VkQueueFamilyProperties *families=calloc(n,sizeof *families);
 vkGetPhysicalDeviceQueueFamilyProperties(context.phys,&n,families);uint32_t q=0;while(q<n && !(families[q].queueFlags&VK_QUEUE_COMPUTE_BIT))q++;CHECK(q<n);free(families);
 float priority=1;VkDeviceQueueCreateInfo qi={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,.queueFamilyIndex=q,.queueCount=1,.pQueuePriorities=&priority};
 VkPhysicalDeviceSynchronization2Features sync={.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES,.synchronization2=VK_TRUE};
 const char *push=VK_KHR_PUSH_DESCRIPTOR_EXTENSION_NAME;
 VkDeviceCreateInfo dci={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,.pNext=&sync,.queueCreateInfoCount=1,.pQueueCreateInfos=&qi,
  .enabledExtensionCount=1,.ppEnabledExtensionNames=&push};VK(vkCreateDevice(context.phys,&dci,NULL,&context.device));
 VkQueue queue;vkGetDeviceQueue(context.device,q,0,&queue);
 VkCommandPoolCreateInfo pi={.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,.queueFamilyIndex=q};
 VkCommandPool pool;VK(vkCreateCommandPool(context.device,&pi,NULL,&pool));
 VkCommandBufferAllocateInfo ca={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,.commandPool=pool,.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY,.commandBufferCount=1};
 VkCommandBuffer cb;VK(vkAllocateCommandBuffers(context.device,&ca,&cb));
 VmaAllocation source_allocation,read_allocation;void *source_map,*read_map;
 VkBuffer source=test_buffer(4096,VK_BUFFER_USAGE_STORAGE_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,&source_allocation,&source_map);
 VkBuffer read=test_buffer(2*QUAD_INDEX_BYTES,VK_BUFFER_USAGE_TRANSFER_DST_BIT,&read_allocation,&read_map);
 memset(source_map,0xa5,4096);
 const uint32_t cases[][4]={{8,1,0,0},{12,3,11,7},{7,2,4,3},{3,9,0xffffffffu,0xffffffffu},{0,1,0,0},
  {8,0,0xffffffffu,0xffffffffu},{1048576,1,31,17},{20,2,123,41}};
 unsigned checks=0;
 for(unsigned test=0;test<sizeof cases/sizeof *cases;test+=2){
  VkExec ex={.cb=cb,.fence=test+1};VK(vkResetCommandBuffer(cb,0));
  VkCommandBufferBeginInfo bi={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};VK(vkBeginCommandBuffer(cb,&bi));
#ifdef QUAD_RECORDER_TEST
  rec_begin(cb);
#endif
  VkQuadIndirect output[2];uint32_t args[2][4];QuadBlock *blocks[2];uint32_t slots[2];
  for(unsigned i=0;i<2;i++){
   memcpy(args[i],cases[test+i],16);if(invalid && !test && !i)args[i][0]=1048580;
   // This transfer is the producer: the CPU-visible source bytes are not the arguments.
   uint32_t transferred[4];memcpy(transferred,args[i],16);uint32_t argument_offset=36+i*4;
   vkCmdUpdateBuffer(cb,source,argument_offset,16,transferred);memset(transferred,0xdd,sizeof transferred);
   VkMemSpan span={.buffer=source,.offset=argument_offset,.avail=16};CHECK(vk_quad_indirect_prepare(&ex,&span,0x12340000+i*16,&output[i]));
   blocks[i]=current;slots[i]=current->used-1;
   uint32_t count=args[i][0]&~3u;
   if(!invalid && count && args[i][1]){
    VkMemoryBarrier2 m={.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER_2,.srcStageMask=VK_PIPELINE_STAGE_2_COMPUTE_SHADER_BIT,
     .srcAccessMask=VK_ACCESS_2_SHADER_STORAGE_WRITE_BIT,.dstStageMask=VK_PIPELINE_STAGE_2_COPY_BIT,.dstAccessMask=VK_ACCESS_2_TRANSFER_READ_BIT};
    VkDependencyInfo d={.sType=VK_STRUCTURE_TYPE_DEPENDENCY_INFO,.memoryBarrierCount=1,.pMemoryBarriers=&m};vkCmdPipelineBarrier2(cb,&d);
    VkBufferCopy copy={0,i*QUAD_INDEX_BYTES,(VkDeviceSize)(count/4)*6*4};vkCmdCopyBuffer(cb,output[i].indices,read,1,&copy);
   }
  }
  VkMemoryBarrier2 host={.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER_2,.srcStageMask=VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
   .srcAccessMask=VK_ACCESS_2_MEMORY_WRITE_BIT,.dstStageMask=VK_PIPELINE_STAGE_2_HOST_BIT,.dstAccessMask=VK_ACCESS_2_HOST_READ_BIT};
  VkDependencyInfo dependency={.sType=VK_STRUCTURE_TYPE_DEPENDENCY_INFO,.memoryBarrierCount=1,.pMemoryBarriers=&host};vkCmdPipelineBarrier2(cb,&dependency);
#ifdef QUAD_RECORDER_TEST
  CHECK(SetEvent(consumer_gate));if(!rec_end_cb(cb,0,0))VK(vkEndCommandBuffer(cb));
#else
  VK(vkEndCommandBuffer(cb));
#endif
  VkSubmitInfo submit={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&cb};
  VK(vkQueueSubmit(queue,1,&submit,VK_NULL_HANDLE));VK(vkQueueWaitIdle(queue));
  if(invalid){for(unsigned i=0;i<ncallbacks;i++)callbacks[i].fn(callbacks[i].arg);CHECK(0);}
  for(unsigned i=0;i<2;i++){
   uint32_t *h=(uint32_t *)(blocks[i]->mapped+slots[i]*Q.stride),count=args[i][0]&~3u;
   int active=count && args[i][1];CHECK(h[0]==(active?(count/4)*6:0) && h[1]==(active?args[i][1]:0));
   CHECK(h[2]==0 && h[3]==(active?args[i][2]:0) && h[4]==(active?args[i][3]:0) && !h[8] && h[13]==0x51554144u);
   CHECK(!memcmp(h+9,args[i],16));checks+=10;
   if(active){
    const uint32_t pattern[6]={0,1,2,0,2,3};uint32_t *actual=(uint32_t *)((uint8_t *)read_map+i*QUAD_INDEX_BYTES);
    // Fixed indices plus vertexOffset (h[3]) give the vertex the direct path's baked indices gave.
    for(uint32_t j=0;j<(count/4)*6;j++){CHECK(actual[j]==(j/6)*4+pattern[j%6] && actual[j]+h[3]==args[i][2]+(j/6)*4+pattern[j%6]);checks++;}
   }
  }
  for(unsigned i=0;i<ncallbacks;i++)callbacks[i].fn(callbacks[i].arg);ncallbacks=0;
 }
 vk_quad_shutdown();vmaDestroyBuffer(context.vma,source,source_allocation);vmaDestroyBuffer(context.vma,read,read_allocation);CHECK(allocated==freed);
 vkDestroyCommandPool(context.device,pool,NULL);vkDestroyDevice(context.device,NULL);
 PFN_vkDestroyDebugUtilsMessengerEXT destroy_debug=(PFN_vkDestroyDebugUtilsMessengerEXT)vkGetInstanceProcAddr(context.instance,"vkDestroyDebugUtilsMessengerEXT");
 destroy_debug(context.instance,messenger,NULL);vkDestroyInstance(context.instance,NULL);CHECK(!validation_errors);
 printf("GPU QUADS: %u exact checks, no validation errors\n",checks);return 0;
}
'''
    return module + support + main


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "local Vulkan GPU and compiler required")
class GpuQuadTests(unittest.TestCase):
    def test_gpu_producer_shared_index_reuse_bounds_and_retained_headers(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-gpu-quads-") as tmp:
            folder = Path(tmp); path = folder / "test.c"; path.write_text(source(), encoding="utf-8"); exe = folder / "test.exe"
            command = [str(CC), "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers", "-march=x86-64-v3",
                       "-I", str(ROOT / "runtime"), "-I", str(DEPS / "include"), "-I", str(DEPS / "include/vma"),
                       str(path), "-L", str(DEPS / "lib"), "-lvulkan-1", "-o", str(exe)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            env = dict(os.environ); env["VK_LAYER_PATH"] = "F:/VulkanValidationLayers/ucrt64/bin"
            for mode, expected in (("good", 0), ("bad", 73)):
                try:
                    result = subprocess.run([str(exe), mode], cwd=folder, env=env, capture_output=True, text=True, timeout=20)
                except subprocess.TimeoutExpired as error:
                    self.fail(f"GPU fixture timeout: {error.stdout!r} {error.stderr!r}")
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn("no validation errors" if mode == "good" else "GPU QUADS indirect validation failed", result.stdout + result.stderr)

    def test_production_recorder_owns_push_and_transfer_payloads(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-quad-recorder-") as tmp:
            folder = Path(tmp); path = folder / "test.c"; path.write_text(source(True), encoding="utf-8"); exe = folder / "test.exe"
            result = subprocess.run([str(CC), "-O2", "-DQUAD_RECORDER_TEST", "-Wall", "-Wextra", "-Werror",
                "-Wno-missing-field-initializers", "-march=x86-64-v3", "-I", str(ROOT / "runtime"),
                "-I", str(DEPS / "include"), "-I", str(DEPS / "include/vma"), str(path), str(ROOT / "runtime/vk_rec.c"),
                "-L", str(DEPS / "lib"), "-lvulkan-1", "-o", str(exe)], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            env = dict(os.environ); env["VK_LAYER_PATH"] = "F:/VulkanValidationLayers/ucrt64/bin"
            result = subprocess.run([str(exe), "good"], cwd=folder, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("no validation errors", result.stdout)


if __name__ == "__main__":
    unittest.main()
