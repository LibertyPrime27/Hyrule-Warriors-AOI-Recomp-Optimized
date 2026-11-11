// Bounded read-only vertex snapshots for imported pools without a full VRAM mirror.
#pragma once
#include "vk_exec.h"

int vk_vertex_cache_prepare(VkExec *ex, uint32_t streams); // copies outside a pass; 1 when the caller must reopen it
int vk_vertex_cache_active(void);
void vk_vertex_cache_buffer_written(const VkMemSpan *span, uint64_t size);
int vk_vertex_cache_lookup(unsigned binding, VkDescriptorBufferInfo *out);
uint64_t vk_vertex_cache_generation(void);
void vk_vertex_cache_shutdown(void);
void vk_vertex_cache_pool_shutdown(void); // device idle, after deferred buffers are collected
