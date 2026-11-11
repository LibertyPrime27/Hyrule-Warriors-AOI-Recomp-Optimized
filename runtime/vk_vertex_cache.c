// Original bounded GPU snapshots. Source copies run in graphics queue order, so
// compute-produced vertices need no CPU readback. Only read-only ordinary draws
// are admitted by the caller; unsupported forms retain their imported buffers.
#include <string.h>
#include "cfg.h"
#include "host.h"
#include "vk_vertex_cache.h"

#define VERTEX_ENTRIES 1024
#define VERTEX_BUDGET (UINT64_C(512) << 20)
#define VERTEX_MAX_RANGE (UINT64_C(64) << 20)
#define VERTEX_BLOCK (UINT64_C(128) << 20)
#define VERTEX_PHYSICAL_BUDGET (UINT64_C(1024) << 20)
typedef struct {
    uint64_t address, size, serial, source_offset, used, gpu_epoch, checked_fence;
    uint32_t cpu_generation;
    VkBuffer source, buffer;
    VmaAllocation allocation;
} VertexEntry;
static VertexEntry entries[VERTEX_ENTRIES];
static VkDescriptorBufferInfo prepared[17];
static uint64_t bytes, sequence, generation=1, gpu_epoch=1;
static uint64_t hits, copies, copied_bytes, evictions;
static int cache_mode=-1;
static VmaPool pool;
static int pool_tried;
static uint64_t last_log;
int vk_vertex_cache_active(void) {
    if(cache_mode<0)cache_mode=rt_knob("VERTEX_CACHE","0");
    return RT_KNOB_ON(cache_mode);
}

void vk_vertex_cache_written(void) { if(cache_mode>0)__atomic_add_fetch(&gpu_epoch, 1, __ATOMIC_RELAXED); }
void vk_vertex_cache_buffer_written(const VkMemSpan *span,uint64_t size) {
    if(cache_mode<=0 || !size)return;
    if(!span || !span->buffer || span->offset>UINT64_MAX-size){vk_vertex_cache_written();return;}
    // Compare resolved buffer offsets, not guest virtual addresses: aliases of
    // the same imported storage must invalidate each other too. Bind-time
    // marking precedes compute dispatch; snapshots are taken only for draws.
    for(unsigned i=0;i<VERTEX_ENTRIES;i++){
        VertexEntry *e=&entries[i];
        if(e->buffer && e->source==span->buffer && e->source_offset<span->offset+size &&
            span->offset<e->source_offset+e->size)e->gpu_epoch=0;
    }
}
uint64_t vk_vertex_cache_generation(void) { return generation; }
int vk_vertex_cache_lookup(unsigned binding, VkDescriptorBufferInfo *out) {
    if (binding >= 17 || !prepared[binding].buffer) return 0;
    *out=prepared[binding];return 1;
}
static void drop_entry(VertexEntry *e) {
    if (!e->buffer) return;
    vk_defer_buffer(e->buffer,e->allocation);
    bytes-=e->size;memset(e,0,sizeof *e);evictions++;generation++;
}
static VertexEntry *entry_for(uint64_t address,uint64_t size,uint64_t serial,VkBuffer source,uint64_t offset) {
    VertexEntry *empty=NULL,*oldest=NULL;
    for(unsigned i=0;i<VERTEX_ENTRIES;i++){
        VertexEntry *e=&entries[i];
        if(e->buffer && e->address==address && e->size==size && e->serial==serial && e->source==source && e->source_offset==offset)return e;
        if(!e->buffer && !empty)empty=e;
        if(e->buffer && (!oldest || e->used<oldest->used))oldest=e;
    }
    while(bytes>VERTEX_BUDGET-size){
        VertexEntry *victim=NULL;
        for(unsigned i=0;i<VERTEX_ENTRIES;i++)if(entries[i].buffer && (!victim || entries[i].used<victim->used))victim=&entries[i];
        if(!victim)return NULL;
        drop_entry(victim);if(!empty)empty=victim;
    }
    if(!empty){empty=oldest;drop_entry(empty);}
    uint64_t used,budget;int exact;
    if(vk_mem_vram(&used,&budget,&exact) && (used>=budget || budget-used<size+(UINT64_C(256)<<20)))return NULL;
    VkBufferCreateInfo bi={.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,.size=size,
        .usage=VK_BUFFER_USAGE_VERTEX_BUFFER_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT};
    VmaAllocationCreateInfo ai={.usage=VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE,.requiredFlags=VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT};
    if(!pool_tried){
        uint32_t type;
        VmaPoolCreateInfo pi={.blockSize=VERTEX_BLOCK,.maxBlockCount=VERTEX_PHYSICAL_BUDGET/VERTEX_BLOCK};
        if(vmaFindMemoryTypeIndexForBufferInfo(vk_ctx()->vma,&bi,&ai,&type)==VK_SUCCESS){
            pi.memoryTypeIndex=type;
            if(vmaCreatePool(vk_ctx()->vma,&pi,&pool)!=VK_SUCCESS)pool=NULL;
        }
        pool_tried=1;
    }
    // Cap physical blocks too: the default allocator may retain several GiB of
    // slack after cache churn. Pending GPU retirements consume this same cap.
    // A full or unavailable pool falls back to the imported source, never an
    // unbounded allocation or a CPU wait for the eviction to finish.
    if(!pool)return NULL;
    ai.pool=pool;
    VkBuffer buffer;VmaAllocation allocation;
    if(vmaCreateBuffer(vk_ctx()->vma,&bi,&ai,&buffer,&allocation,NULL)!=VK_SUCCESS)return NULL;
    *empty=(VertexEntry){.address=address,.size=size,.serial=serial,.source=source,.source_offset=offset,.buffer=buffer,.allocation=allocation};
    bytes+=size;return empty;
}
int vk_vertex_cache_prepare(VkExec *ex,uint32_t streams) {
    int active=vk_vertex_cache_active(); // opt-in until byte/pixel and live A/B validation complete
    if(!cache_mode)return 0;
    VkDescriptorBufferInfo previous[17];memcpy(previous,prepared,sizeof previous);memset(prepared,0,sizeof prepared);
    int copied=0;
    if(ex && ex->fence && active)for(unsigned b=0;b<16;b++){
        if(!(streams&(1u<<b)))continue;
        uint64_t address=ex->vbuf[b].address,n=ex->vbuf[b].size;
        if(!address || n<4096 || n>VERTEX_MAX_RANGE || (address&3) || (n&3) || address>UINT64_MAX-n)continue;
        VkMemSpan span;VkMemInspect backing;
        if(!vk_mem_resolve(address,n,&span) || !span.pool || !span.buffer || span.avail<n || (span.offset&3))continue;
        VK_MEM_USE(span);
        if(!vk_mem_inspect(address,n,&backing) || backing.pool_kind!=VK_MEM_INSPECT_POOL_IMPORT ||
            backing.mirror_state==VK_MEM_INSPECT_MIRROR_ON || !backing.storage_serial)continue;
        if(vk_texture_cpu_stale(address,n))continue; // a texture's newer image is not represented by this buffer
        VertexEntry *e=entry_for(address,n,backing.storage_serial,span.buffer,span.offset);if(!e)continue;
        uint64_t epoch=__atomic_load_n(&gpu_epoch,__ATOMIC_RELAXED);
        uint32_t now=e->cpu_generation;
        int changed=e->checked_fence!=ex->fence ? vk_mem_changed(address,n,e->cpu_generation,&now) : 0;
        if(!e->used || changed || e->gpu_epoch!=epoch){
            if(!copied)vk_exec_barrier(ex); // source producers precede all snapshots; never copy inside a pass
            VkBufferCopy copy={span.offset,0,n};vkCmdCopyBuffer(ex->cb,span.buffer,e->buffer,1,&copy);
            ex->recorded++;copies++;copied_bytes+=n;copied=1;
            e->gpu_epoch=epoch;e->cpu_generation=now;
        }else hits++;
        e->used=++sequence;e->checked_fence=ex->fence;
        prepared[b]=(VkDescriptorBufferInfo){e->buffer,0,n};VK_MEM_BOUND(span);
    }
    if(copied){
        VkMemoryBarrier2 memory={.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER_2,
            .srcStageMask=VK_PIPELINE_STAGE_2_COPY_BIT,.srcAccessMask=VK_ACCESS_2_TRANSFER_WRITE_BIT,
            .dstStageMask=VK_PIPELINE_STAGE_2_VERTEX_INPUT_BIT,.dstAccessMask=VK_ACCESS_2_VERTEX_ATTRIBUTE_READ_BIT};
        VkDependencyInfo di={.sType=VK_STRUCTURE_TYPE_DEPENDENCY_INFO,.memoryBarrierCount=1,.pMemoryBarriers=&memory};
        vkCmdPipelineBarrier2(ex->cb,&di);ex->recorded++;
    }
    if(copied || memcmp(previous,prepared,sizeof prepared))generation++;
    uint64_t frame=__atomic_load_n(&gfx_counters.frames,__ATOMIC_RELAXED);
    if(cache_mode && frame/600!=last_log){
        last_log=frame/600;
        VmaStatistics stats={0};if(pool)vmaGetPoolStatistics(vk_ctx()->vma,pool,&stats);
        rt_log("vertex-cache: frame=%llu live_mib=%.1f reserved_mib=%.1f allocated_mib=%.1f hits=%llu copies=%llu copied_mib=%.1f evictions=%llu",
            (unsigned long long)frame,bytes/1048576.0,stats.blockBytes/1048576.0,stats.allocationBytes/1048576.0,
            (unsigned long long)hits,(unsigned long long)copies,
            copied_bytes/1048576.0,(unsigned long long)evictions);
    }
    return copied;
}
void vk_vertex_cache_shutdown(void) {
    for(unsigned i=0;i<VERTEX_ENTRIES;i++)drop_entry(&entries[i]);
    memset(prepared,0,sizeof prepared);bytes=sequence=hits=copies=copied_bytes=evictions=0;generation++;
}
void vk_vertex_cache_pool_shutdown(void) {
    // Called only after device idle and vk_memory's deferred buffers have been
    // destroyed. The pool must outlive every buffer evicted from this cache.
    if(pool)vmaDestroyPool(vk_ctx()->vma,pool);
    pool=NULL;pool_tried=0;cache_mode=-1;gpu_epoch=1;last_log=0;
}
