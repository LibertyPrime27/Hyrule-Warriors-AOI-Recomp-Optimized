// Original GPU argument/index adapter. No game shader, address or patch is used.
// GPU-written arguments stay on the GPU; invalid bounded forms draw nothing and
// report a fatal error from their completion callback, without a CPU fence stall.
// Indices are a fixed pattern 4q + {0,1,2,0,2,3} written once at init; each draw's converted command carries its
// firstVertex as vertexOffset, so VertexIndex is firstVertex + 4q + {0,1,2,0,2,3} exactly as the direct QUADS path
// gives. (BaseVertex differs from that path's 0, but no translated shader reads it: Ryujinx maps VertexId to
// VertexIndex and the runtime's c0 carries no base vertex.) A draw costs one tiny dispatch and no index writes, where a
// per-draw expansion into one shared buffer cost a second dispatch, a third barrier and a wait on the previous draw.
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "host.h"
#include "vk_quad.h"
#include "platform/platform.h"
#include "quad_prepare_spv.h"

#define QUAD_SLOTS 64u
#define QUAD_SPARES 32u
#define QUAD_INDEX_BYTES (UINT64_C(1048576) / 4 * 6 * 4)

typedef struct QuadBlock {
    struct QuadBlock *next;
    VkBuffer buffer;
    VmaAllocation allocation;
    uint8_t *mapped;
    uint64_t fence, address[QUAD_SLOTS];
    uint8_t submitted[QUAD_SLOTS];
    uint32_t used;
} QuadBlock;

static struct {
    int tried, ready;
    VkDevice device;
    VkDescriptorSetLayout set;
    VkPipelineLayout layout;
    VkPipeline prepare;
    VkBuffer indices;
    VmaAllocation allocation;
    PFN_vkCmdPushDescriptorSetKHR push;
    uint64_t stride;
} Q;
static PlatMutex quad_lock = PLAT_MUTEX_INIT;
static QuadBlock *spares, *current;
static uint32_t spare_count;
static uint64_t current_fence;

static void block_free(QuadBlock *b) {
    vmaDestroyBuffer(vk_ctx()->vma, b->buffer, b->allocation);
    free(b);
}

static void block_done(void *arg) {
    QuadBlock *b = arg;
    for (uint32_t i = 0; i < b->used; i++) {
        if (!b->submitted[i]) continue;
        const volatile uint32_t *w = (const volatile uint32_t *)(b->mapped + i * Q.stride);
        if (w[13] != 0x51554144u || w[8])
            rt_fatal("vk: GPU QUADS indirect validation failed: source=%#llx status=%#x marker=%#x "
                     "vertices=%u instances=%u first=%u base=%u",
                     (unsigned long long)b->address[i], w[8], w[13], w[9], w[10], w[11], w[12]);
    }
    plat_mutex_lock(&quad_lock);
    int keep = spare_count < QUAD_SPARES;
    if (keep) { b->next = spares; spares = b; spare_count++; }
    plat_mutex_unlock(&quad_lock);
    if (!keep) block_free(b);
}

static int quad_pipeline(const uint32_t *words, size_t bytes, VkPipeline *pipeline) {
    VkCtx *c = vk_ctx();
    VkShaderModuleCreateInfo mi = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = bytes, .pCode = words};
    VkShaderModule module;
    if (vkCreateShaderModule(c->device, &mi, NULL, &module) != VK_SUCCESS) return 0;
    VkComputePipelineCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO, .layout = Q.layout,
        .stage = {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_COMPUTE_BIT,
                  .module = module, .pName = "main"}};
    VkResult result = vkCreateComputePipelines(c->device, VK_NULL_HANDLE, 1, &ci, NULL, pipeline);
    vkDestroyShaderModule(c->device, module, NULL);
    return result == VK_SUCCESS;
}

static int quad_init(void) {
    if (Q.tried) return Q.ready;
    Q.tried = 1;
    VkCtx *c = vk_ctx(); Q.device = c->device;
    Q.push = (PFN_vkCmdPushDescriptorSetKHR)vkGetDeviceProcAddr(c->device, "vkCmdPushDescriptorSetKHR");
    if (!Q.push) return 0;
    Q.stride = c->props.limits.minStorageBufferOffsetAlignment;
    if (Q.stride < 256) Q.stride = 256;
    VkDescriptorSetLayoutBinding binding[2];
    memset(binding, 0, sizeof binding);
    for (uint32_t i = 0; i < 2; i++) binding[i] = (VkDescriptorSetLayoutBinding){.binding = i,
        .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .descriptorCount = 1, .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT};
    VkDescriptorSetLayoutCreateInfo si = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .flags = VK_DESCRIPTOR_SET_LAYOUT_CREATE_PUSH_DESCRIPTOR_BIT_KHR, .bindingCount = 2, .pBindings = binding};
    if (vkCreateDescriptorSetLayout(c->device, &si, NULL, &Q.set) != VK_SUCCESS) return 0;
    VkPushConstantRange range = {.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT, .offset = 0, .size = 4};
    VkPipelineLayoutCreateInfo li = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount = 1, .pSetLayouts = &Q.set, .pushConstantRangeCount = 1, .pPushConstantRanges = &range};
    if (vkCreatePipelineLayout(c->device, &li, NULL, &Q.layout) != VK_SUCCESS) return 0;
    if (!quad_pipeline(quad_prepare_spv, sizeof quad_prepare_spv, &Q.prepare)) return 0;
    // The fixed index pattern, written once: never a write hazard for any draw.
    VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = QUAD_INDEX_BYTES,
        .usage = VK_BUFFER_USAGE_INDEX_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT};
    VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE,
        .requiredFlags = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT,
        .flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT};
    VmaAllocationInfo info;
    if (vmaCreateBuffer(c->vma, &bi, &ai, &Q.indices, &Q.allocation, &info) != VK_SUCCESS) return 0;
    if (!info.pMappedData) return 0;
    uint32_t *ix = info.pMappedData;
    static const uint32_t pattern[6] = {0, 1, 2, 0, 2, 3};
    for (uint64_t k = 0; k < QUAD_INDEX_BYTES / 4; k++) ix[k] = (uint32_t)(k / 6 * 4) + pattern[k % 6];
    if (vmaFlushAllocation(c->vma, Q.allocation, 0, VK_WHOLE_SIZE) != VK_SUCCESS) return 0;
    Q.ready = 1;
    rt_log("vk: GPU QUADS indirect conversion ready; argument readback is unnecessary for NVN2 graphics");
    return 1;
}

static QuadBlock *quad_block(VkExec *ex) {
    // A completion callback can free an old block. Never dereference current
    // after advancing to another fence, even when the spare cache was full.
    if (current_fence != ex->fence) current = NULL, current_fence = ex->fence;
    if (current && current->used < QUAD_SLOTS) return current;
    plat_mutex_lock(&quad_lock);
    QuadBlock *b = spares;
    if (b) spares = b->next, spare_count--;
    plat_mutex_unlock(&quad_lock);
    if (!b) {
        b = calloc(1, sizeof *b);
        if (!b) return NULL;
        VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = Q.stride * QUAD_SLOTS,
            .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_INDIRECT_BUFFER_BIT};
        VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_HOST,
            .requiredFlags = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            .flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT};
        VmaAllocationInfo info;
        if (vmaCreateBuffer(vk_ctx()->vma, &bi, &ai, &b->buffer, &b->allocation, &info) != VK_SUCCESS) { free(b); return NULL; }
        b->mapped = info.pMappedData;
    }
    b->used = 0; b->fence = ex->fence; memset(b->submitted, 0, sizeof b->submitted);
    vk_completion_add(ex->fence, block_done, b);
    current = b;
    return b;
}

// The source binding of an argument record: its storage offset rounded down to the device alignment and the word
// offset of the record within it. 0 when the record cannot be bound.
static int source_binding(const VkMemSpan *source, uint64_t *offset, uint64_t *lead) {
    if (!source || !source->buffer || source->avail < 16 || (source->offset & 3) || source->offset > UINT64_MAX - 16)
        return 0;
    VkCtx *c = vk_ctx();
    uint64_t alignment = c->props.limits.minStorageBufferOffsetAlignment;
    if (!alignment) alignment = 1;
    *offset = source->offset / alignment * alignment, *lead = source->offset - *offset;
    return !(*lead & 3) && *lead / 4 <= UINT32_MAX && *lead + 16 <= c->props.limits.maxStorageBufferRange;
}

int vk_quad_indirect_prepare_batch(VkExec *ex, const VkMemSpan *sources, const uint64_t *addresses, uint32_t n,
                                   VkQuadIndirect *out, uint8_t *ok) {
    if (!ex || !ex->fence || !sources || !addresses || !out || !ok || !n || !quad_init()) return 0;
    uint32_t done = 0;
    for (uint32_t i = 0; i < n; i++) {
        uint64_t offset, lead;
        ok[i] = 0;
        if (!source_binding(&sources[i], &offset, &lead)) continue;
        QuadBlock *b = quad_block(ex);
        if (!b) continue;
        if (!done) {
            // Once for the whole batch: the arguments' producers precede every conversion (and dispatches need the
            // render pass ended). Each record converts into its own header slot, so the dispatches need no barriers
            // between them.
            vk_exec_barrier(ex);
            vkCmdBindPipeline(ex->cb, VK_PIPELINE_BIND_POINT_COMPUTE, Q.prepare);
            ex->recorded += 2;
        }
        uint32_t slot = b->used++;
        uint64_t header = slot * Q.stride;
        memset(b->mapped + header, 0, 64); b->address[slot] = addresses[i];
        VkDescriptorBufferInfo buffer[2] = {{sources[i].buffer, offset, lead + 16}, {b->buffer, header, 64}};
        VkWriteDescriptorSet write[2]; memset(write, 0, sizeof write);
        for (uint32_t k = 0; k < 2; k++) write[k] = (VkWriteDescriptorSet){.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
            .dstBinding = k, .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .pBufferInfo = &buffer[k]};
        rec_push(Q.push, ex->cb, VK_PIPELINE_BIND_POINT_COMPUTE, Q.layout, 0, 2, write);
        uint32_t word = (uint32_t)(lead / 4);
        vkCmdPushConstants(ex->cb, Q.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, 4, &word);
        vkCmdDispatch(ex->cb, 1, 1, 1);
        b->submitted[slot] = 1;
        ex->recorded += 3;
        out[i] = (VkQuadIndirect){Q.indices, b->buffer, header};
        ok[i] = 1;
        done++;
    }
    if (!done) return 0;
    VkMemoryBarrier2 memory = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER_2,
        .srcStageMask = VK_PIPELINE_STAGE_2_COMPUTE_SHADER_BIT, .srcAccessMask = VK_ACCESS_2_SHADER_STORAGE_WRITE_BIT,
        .dstStageMask = VK_PIPELINE_STAGE_2_DRAW_INDIRECT_BIT | VK_PIPELINE_STAGE_2_HOST_BIT,
        .dstAccessMask = VK_ACCESS_2_INDIRECT_COMMAND_READ_BIT | VK_ACCESS_2_HOST_READ_BIT};
    VkDependencyInfo di = {.sType = VK_STRUCTURE_TYPE_DEPENDENCY_INFO, .memoryBarrierCount = 1, .pMemoryBarriers = &memory};
    vkCmdPipelineBarrier2(ex->cb, &di);
    ex->recorded += 1;
    return (int)done;
}

int vk_quad_indirect_prepare(VkExec *ex, const VkMemSpan *source, uint64_t address, VkQuadIndirect *out) {
    uint8_t ok = 0;
    return source && vk_quad_indirect_prepare_batch(ex, source, &address, 1, out, &ok) == 1 && ok;
}

void vk_quad_shutdown(void) {
    VkCtx *c = vk_ctx();
    if (!c) return;
    while (spares) { QuadBlock *b = spares; spares = b->next; block_free(b); }
    spare_count = 0; current = NULL; current_fence = 0;
    if (Q.indices) vmaDestroyBuffer(c->vma, Q.indices, Q.allocation);
    if (Q.prepare) vkDestroyPipeline(c->device, Q.prepare, NULL);
    if (Q.layout) vkDestroyPipelineLayout(c->device, Q.layout, NULL);
    if (Q.set) vkDestroyDescriptorSetLayout(c->device, Q.set, NULL);
    memset(&Q, 0, sizeof Q);
}
