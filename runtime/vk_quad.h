// Original GPU-side adapter for array QUADS indirect draws.
#pragma once
#include "vk_exec.h"
typedef struct { VkBuffer indices, arguments; VkDeviceSize offset; } VkQuadIndirect;
// Replay thread only. Source span is already resolved/synchronized by the caller.
// Zero means no converter commands were recorded; the caller can use its CPU fallback.
int vk_quad_indirect_prepare(VkExec *ex, const VkMemSpan *source, uint64_t source_address, VkQuadIndirect *out);
// n records converted behind one barrier pair (one render-pass end for the whole batch). ok[i] says whether out[i]
// was recorded; returns how many were. The records must not be written by anything recorded after this call and
// before their draws (vk_exec_draw.c only batches draws separated by pure state commands).
int vk_quad_indirect_prepare_batch(VkExec *ex, const VkMemSpan *sources, const uint64_t *source_addresses, uint32_t n,
                                   VkQuadIndirect *out, uint8_t *ok);
void vk_quad_shutdown(void); // after submission completion callbacks have drained
