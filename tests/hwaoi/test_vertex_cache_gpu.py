"""Actual GPU byte equality, source mutations and bounded read-only snapshot lifetimes."""
import os
from pathlib import Path
import subprocess,sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests/hwaoi'))
import test_gpu_quads as harness
CC=Path(os.environ.get("HWAOI_TOOLS", "F:") + '/llvm-mingw/bin/clang.exe');DEPS=Path(os.environ.get('HWAOI_TEST_DEPS') or 'F:/TotKDependencies/runtime/deps')   # HWAOI_TEST_DEPS: a deps tree with include/ and lib/

def source():
    base=harness.source(False)
    support=base[base.index('#include <stdio.h>'):base.index('static unsigned validation_errors;')]
    # Exercise the real allocator, including its physical block limit and the
    # lifetime of allocations awaiting GPU completion.
    start=support.index('typedef struct {VkDeviceMemory memory;void *map;} Allocation;')
    end=support.index('typedef struct{void(*fn)(void*);void *arg;} Callback;')
    support=support[:start]+support[end:]
    setup=base[base.index('static unsigned validation_errors;'):base.index(' VmaAllocation source_allocation,read_allocation;')]
    setup=setup.replace('context.vma=(VmaAllocator)(uintptr_t)1;', '')
    setup=setup.replace(' VkQueue queue;', '''
 VmaAllocatorCreateInfo allocator_info={.physicalDevice=context.phys,.device=context.device,
  .instance=context.instance,.vulkanApiVersion=VK_API_VERSION_1_2};
 VK(vmaCreateAllocator(&allocator_info,&context.vma));
 VkQueue queue;''')
    # Reuse the independent Vulkan allocation/validation harness; the production
    # module below performs every cache decision and records every snapshot.
    module=(ROOT/'runtime/vk_vertex_cache.c').read_text(encoding='utf-8')
    module=module.replace('#include "vk_vertex_cache.h"','#include "vk_vertex_cache.h"\n#undef vkCmdCopyBuffer\n#undef vkCmdPipelineBarrier2\n#undef vkCmdUpdateBuffer')
    extra=r'''
static VkBuffer source_buffer;static uint64_t source_bytes;
static uint64_t storage_lifetime=1;static uint32_t cpu_gen=1;
static int changed_policy,texture_stale,mirror_active,backed=1,ab_phase=1;
int vk_mirror_on;uint32_t rt_trace;GfxStats gfx_counters;
int rt_knob(const char *name,const char *def){CHECK(!strcmp(name,"VERTEX_CACHE")&&!strcmp(def,"0"));return 2;}
int rt_ab(void){return ab_phase;}
int vk_mem_resolve(uint64_t a,uint64_t n,VkMemSpan *out){
 if(!backed||a<0x10000000||a-0x10000000>source_bytes||n>source_bytes-(a-0x10000000))return 0;
 *out=(VkMemSpan){.buffer=source_buffer,.offset=a-0x10000000,.avail=source_bytes-(a-0x10000000),.pool=(void*)1};return 1;}
int vk_mem_inspect(uint64_t a,uint64_t n,VkMemInspect *out){VkMemSpan s;if(!vk_mem_resolve(a,n,&s))return 0;
 *out=(VkMemInspect){.offset=s.offset,.avail=s.avail,.storage_serial=storage_lifetime,
  .pool_kind=VK_MEM_INSPECT_POOL_IMPORT,.mirror_state=mirror_active?VK_MEM_INSPECT_MIRROR_ON:VK_MEM_INSPECT_MIRROR_NONE};return 1;}
int vk_mem_changed(uint64_t a,uint64_t n,uint32_t since,uint32_t *now){(void)a;(void)n;*now=cpu_gen;return changed_policy?changed_policy:cpu_gen>since;}
int vk_texture_cpu_stale(uint64_t a,uint64_t n){(void)a;(void)n;return texture_stale;}
int vk_mem_vram(uint64_t *used,uint64_t *budget,int *exact){*used=0;*budget=8ull<<30;*exact=1;return 1;}
void vk_mem_use(VkMemSpan *s){(void)s;CHECK(0);}
void vk_mem_bound_note(void *p){(void)p;}
typedef struct{VkBuffer buffer;VmaAllocation allocation;} Retired;
static Retired retired[VERTEX_ENTRIES+32];static unsigned nretired;
void vk_defer_buffer(VkBuffer b,VmaAllocation a){CHECK(nretired<VERTEX_ENTRIES+32);retired[nretired++]=(Retired){b,a};}
static void collect(void){for(unsigned i=0;i<nretired;i++)vmaDestroyBuffer(context.vma,retired[i].buffer,retired[i].allocation);nretired=0;}
static void full_barrier(VkCommandBuffer cb){VkExec ex={.cb=cb};vk_exec_barrier(&ex);}
'''
    main=r'''
 (void)invalid;
 source_bytes=(VERTEX_ENTRIES+32)*4096;
 VmaAllocation source_alloc,read_alloc;void *source_map,*read_map;
 source_buffer=test_buffer(source_bytes,VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,&source_alloc,&source_map);
 VkBuffer read=test_buffer(8192,VK_BUFFER_USAGE_TRANSFER_DST_BIT,&read_alloc,&read_map);
 for(unsigned i=0;i<source_bytes/4;i++)((uint32_t*)source_map)[i]=i*0x9e3779b9u+17;
 unsigned checks=0;VkBuffer original=VK_NULL_HANDLE;
 for(unsigned round=0;round<8;round++){
  VK(vkResetCommandBuffer(cb,0));VkCommandBufferBeginInfo begin={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};VK(vkBeginCommandBuffer(cb,&begin));
  VkExec ex={.cb=cb,.fence=round+1};ex.vbuf[0]=(NVNbufferRange){.address=0x10000000,.size=4096};
  if(round==2){for(unsigned j=0;j<1024;j++)((uint32_t*)source_map)[j]=j^0x12345678u;cpu_gen++;}
  uint32_t gpu_data[1024];
  if(round==3){for(unsigned j=0;j<1024;j++)gpu_data[j]=j^0xfedcba98u;vkCmdUpdateBuffer(cb,source_buffer,0,4096,gpu_data);vk_vertex_cache_written();}
  if(round==4)storage_lifetime++; // same address/driver handle, a new backing lifetime
  if(round==5)mirror_active=1;
  if(round==6)mirror_active=0,texture_stale=1;
  if(round==7)texture_stale=0,changed_policy=-1; // unknown CPU tracking refreshes, never assumes clean
  uint64_t before=copies;int copied=vk_vertex_cache_prepare(&ex,1);VkDescriptorBufferInfo out;
  if(round==5||round==6){CHECK(!copied&&!vk_vertex_cache_lookup(0,&out));}
  else{
   CHECK(vk_vertex_cache_lookup(0,&out)&&out.range==4096&&!out.offset);
   if(round==0)original=out.buffer;
   if(round==1)CHECK(!copied&&copies==before&&out.buffer==original);
   else CHECK(copied&&copies==before+1);
   if(round==4)CHECK(out.buffer!=original&&!nretired); // distinct backing lifetime, old entry still retained
   full_barrier(cb);VkBufferCopy copy={0,0,4096};vkCmdCopyBuffer(cb,out.buffer,read,1,&copy);
  }
  VkMemoryBarrier2 host={.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER_2,.srcStageMask=VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
   .srcAccessMask=VK_ACCESS_2_MEMORY_WRITE_BIT,.dstStageMask=VK_PIPELINE_STAGE_2_HOST_BIT,.dstAccessMask=VK_ACCESS_2_HOST_READ_BIT};
  VkDependencyInfo di={.sType=VK_STRUCTURE_TYPE_DEPENDENCY_INFO,.memoryBarrierCount=1,.pMemoryBarriers=&host};vkCmdPipelineBarrier2(cb,&di);
  VK(vkEndCommandBuffer(cb));VkSubmitInfo submit={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&cb};
  VK(vkQueueSubmit(queue,1,&submit,VK_NULL_HANDLE));VK(vkQueueWaitIdle(queue));
  if(round!=5&&round!=6){CHECK(!memcmp(read_map,round==3?gpu_data:source_map,4096));checks+=1024;}
  collect();
 }
 // Writes to another range/buffer preserve a hit. An overlapping producer in
 // the SAME fence invalidates both virtual aliases before the second draw.
 changed_policy=0;
 VK(vkResetCommandBuffer(cb,0));VkCommandBufferBeginInfo begin={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};VK(vkBeginCommandBuffer(cb,&begin));
 VkExec mutation={.cb=cb,.fence=88};mutation.vbuf[0]=(NVNbufferRange){.address=0x10000000,.size=4096};
 vk_vertex_cache_prepare(&mutation,1);VkDescriptorBufferInfo snapshot;CHECK(vk_vertex_cache_lookup(0,&snapshot));
 uint32_t before_source[1024],replacement[1024];memcpy(before_source,source_map,4096);
 for(unsigned i=0;i<1024;i++)replacement[i]=i^0x4fab98cdu;
 full_barrier(cb);VkBufferCopy first={0,0,4096};vkCmdCopyBuffer(cb,snapshot.buffer,read,1,&first);
 VkMemSpan producer={.buffer=source_buffer,.offset=4096};uint64_t copied_before=copies;
 vk_vertex_cache_buffer_written(&producer,4096);CHECK(!vk_vertex_cache_prepare(&mutation,1)&&copies==copied_before);
 producer.buffer=read;producer.offset=0;vk_vertex_cache_buffer_written(&producer,4096);
 CHECK(!vk_vertex_cache_prepare(&mutation,1)&&copies==copied_before);
 VertexEntry *alias=entry_for(0x90000000,4096,storage_lifetime,source_buffer,0);CHECK(alias);
 alias->gpu_epoch=gpu_epoch;alias->used=++sequence;
 full_barrier(cb);vkCmdUpdateBuffer(cb,source_buffer,0,4096,replacement);
 producer.buffer=source_buffer;vk_vertex_cache_buffer_written(&producer,4096);CHECK(!alias->gpu_epoch);
 CHECK(vk_vertex_cache_prepare(&mutation,1)&&copies==copied_before+1);CHECK(vk_vertex_cache_lookup(0,&snapshot));
 full_barrier(cb);VkBufferCopy second={0,4096,4096};vkCmdCopyBuffer(cb,snapshot.buffer,read,1,&second);full_barrier(cb);
 VK(vkEndCommandBuffer(cb));VkSubmitInfo submit={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&cb};
 VK(vkQueueSubmit(queue,1,&submit,VK_NULL_HANDLE));VK(vkQueueWaitIdle(queue));
 CHECK(!memcmp(read_map,before_source,4096)&&!memcmp((uint8_t*)read_map+4096,replacement,4096));checks+=2048;
 // Disabling the B arm must clear prepared bindings and invalidate the draw's
 // binding reuse key. Re-enabling must repopulate it even without a copy.
 uint64_t binding_generation=vk_vertex_cache_generation();VkDescriptorBufferInfo probe;
 ab_phase=0;CHECK(!vk_vertex_cache_prepare(NULL,0));CHECK(!vk_vertex_cache_lookup(0,&probe));
 CHECK(vk_vertex_cache_generation()!=binding_generation);ab_phase=1;
 // More unique allocations than entries exercise the production LRU without
 // recording thousands of unrelated barriers into the validation harness.
 VK(vkResetCommandBuffer(cb,0));VK(vkBeginCommandBuffer(cb,&begin));
 for(unsigned i=0;i<VERTEX_ENTRIES+22;i++){
  VertexEntry *entry=entry_for(0x10000000+4096ull*i,4096,storage_lifetime,source_buffer,4096ull*i);
  CHECK(entry);entry->used=++sequence;CHECK(bytes<=VERTEX_BUDGET);checks++;
 }
 CHECK(evictions>0&&nretired>0);
 VK(vkEndCommandBuffer(cb));
 VK(vkQueueSubmit(queue,1,&submit,VK_NULL_HANDLE));VK(vkQueueWaitIdle(queue));collect();
 vk_vertex_cache_shutdown();collect();vk_vertex_cache_pool_shutdown();
 vmaDestroyBuffer(context.vma,source_buffer,source_alloc);
 // Fill the physical budget while keeping evictions pending. Extra requests
 // must fall back without exceeding the cap or destroying GPU users.
 source_bytes=VERTEX_BUDGET;
 source_buffer=test_buffer(source_bytes,VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,&source_alloc,&source_map);
 VK(vkResetCommandBuffer(cb,0));VK(vkBeginCommandBuffer(cb,&begin));
 changed_policy=0;
 unsigned slots=VERTEX_PHYSICAL_BUDGET/VERTEX_MAX_RANGE;
 for(unsigned i=0;i<slots;i++){
  if(i==8)storage_lifetime++;
  VkExec ex={.cb=cb,.fence=100};ex.vbuf[0]=(NVNbufferRange){.address=0x10000000+(i%8)*VERTEX_MAX_RANGE,.size=VERTEX_MAX_RANGE};
  CHECK(vk_vertex_cache_prepare(&ex,1));
 }
 VmaStatistics stats;vmaGetPoolStatistics(context.vma,pool,&stats); // module's pool, not the command pool
 CHECK(stats.blockBytes<=VERTEX_PHYSICAL_BUDGET&&stats.allocationCount==slots);
 VkExec pressure={.cb=cb,.fence=100};pressure.vbuf[0]=(NVNbufferRange){.address=0x10000000,.size=VERTEX_MAX_RANGE};
 storage_lifetime++;CHECK(!vk_vertex_cache_prepare(&pressure,1));CHECK(!vk_vertex_cache_lookup(0,&probe));
 CHECK(nretired>0);vmaGetPoolStatistics(context.vma,pool,&stats);CHECK(stats.blockBytes<=VERTEX_PHYSICAL_BUDGET&&stats.allocationCount==slots);
 VK(vkEndCommandBuffer(cb));VK(vkQueueSubmit(queue,1,&submit,VK_NULL_HANDLE));VK(vkQueueWaitIdle(queue));collect();
 VK(vkResetCommandBuffer(cb,0));VK(vkBeginCommandBuffer(cb,&begin));pressure.fence++;
 CHECK(vk_vertex_cache_prepare(&pressure,1));CHECK(vk_vertex_cache_lookup(0,&probe));
 VK(vkEndCommandBuffer(cb));VK(vkQueueSubmit(queue,1,&submit,VK_NULL_HANDLE));VK(vkQueueWaitIdle(queue));
 vk_vertex_cache_shutdown();collect();vk_vertex_cache_pool_shutdown();
 CHECK(cache_mode==-1&&!bytes&&!pool_tried);
 vmaDestroyBuffer(context.vma,source_buffer,source_alloc);vmaDestroyBuffer(context.vma,read,read_alloc);
 VmaTotalStatistics total;vmaCalculateStatistics(context.vma,&total);CHECK(!total.total.statistics.allocationCount);
 vmaDestroyAllocator(context.vma);vkDestroyCommandPool(context.device,command_pool,NULL);vkDestroyDevice(context.device,NULL);
 PFN_vkDestroyDebugUtilsMessengerEXT destroy=(PFN_vkDestroyDebugUtilsMessengerEXT)vkGetInstanceProcAddr(context.instance,"vkDestroyDebugUtilsMessengerEXT");
 destroy(context.instance,messenger,NULL);vkDestroyInstance(context.instance,NULL);CHECK(!validation_errors);
 printf("vertex snapshots: %u checks, no validation errors\n",checks);return 0;
}
'''
    # The production module calls its VMA pool `pool`; avoid shadowing it with
    # the Vulkan command pool used by the independent fixture.
    setup=setup.replace('VkCommandPool pool;', 'VkCommandPool command_pool;').replace('&pool)', '&command_pool)').replace('.commandPool=pool', '.commandPool=command_pool')
    return '#include "platform/platform.h"\n'+module+support+extra+setup+main

@unittest.skipUnless(os.name=='nt' and CC.is_file(),'Windows Vulkan required')
class VertexCacheGpuTests(unittest.TestCase):
    def test_byte_equality_mutations_lifetime_and_eviction(self):
        with tempfile.TemporaryDirectory(prefix='hwaoi-vertex-cache-') as temp:
            folder=Path(temp);path=folder/'fixture.c';path.write_text(source(),encoding='utf-8');exe=folder/'fixture.exe'
            allocator=folder/'vma.o'
            result=subprocess.run([str(CC.with_name('clang++.exe')),'-O2','-DNDEBUG','-I',str(DEPS/'include'),
                '-I',str(DEPS/'include/vma'),'-c',str(ROOT/'runtime/vk_vma.cpp'),'-o',str(allocator)],
                capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result=subprocess.run([str(CC),'-O2','-Wall','-Wextra','-Werror','-Wno-missing-field-initializers',
                '-I',str(ROOT/'runtime'),'-I',str(DEPS/'include'),'-I',str(DEPS/'include/vma'),str(path),
                str(allocator),'-static-libgcc','-Wl,-Bstatic','-lstdc++','-Wl,-Bdynamic',
                '-L',str(DEPS/'lib'),'-lvulkan-1','-o',str(exe)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            env=dict(os.environ);env['VK_LAYER_PATH']='F:/VulkanValidationLayers/ucrt64/bin'
            result=subprocess.run([str(exe),'good'],cwd=folder,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('no validation errors',result.stdout)

if __name__=='__main__':unittest.main()
