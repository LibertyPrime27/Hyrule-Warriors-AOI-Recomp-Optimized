// SPDX-License-Identifier: MIT
// Most registered single-stage libraries are never requested in a scene. Keep
// those immutable copies compressed; an actual consumer replaces the compressed
// payload with its exact original bytes. Never keep both forms after expansion.
#include <stdlib.h>
#include <string.h>
#include <zstd.h>
#include "cfg.h"
#include "gfx.h"
#include "host.h"
#include "nvn_hmap.h"
#include "nvn_program_source.h"
#include "platform/platform.h"

typedef struct {
    uint32_t raw_bytes, packed_bytes;
    size_t original_bytes, fixed_bytes;
    uint8_t *expanded, *packed;
} ProgramSource;

enum { SOURCE_PREFIX = 128, SOURCE_MAX_CAP = 1u << 17 };
static nvn_hmap sources = {.klen = sizeof(uintptr_t)};
static PlatMutex source_lock = PLAT_MUTEX_INIT;
static int source_mode = -1;
static NvnProgramSourceStats source_memory;

static int source_enabled(void) {
    int mode = __atomic_load_n(&source_mode, __ATOMIC_RELAXED);
    if (mode < 0) {
        mode = cfg_bool("TOTK_SOURCE_COMPRESS");
        __atomic_store_n(&source_mode, mode, __ATOMIC_RELAXED);
    }
    return mode;
}

static int source_reserve(void) { // source_lock held; optional growth must not abort on OOM
    if (((uint64_t)sources.n + 1) * 4 <= (uint64_t)sources.cap * 3) return 1;
    if (sources.cap >= SOURCE_MAX_CAP) return 0;
    nvn_hmap next = {.klen = sizeof(uintptr_t), .cap = sources.cap ? sources.cap * 2 : 64};
    next.keys = malloc((size_t)next.cap * next.klen);
    next.vals = malloc((size_t)next.cap * sizeof *next.vals);
    if (!next.keys || !next.vals) {
        free(next.keys);
        free(next.vals);
        return 0;
    }
    memset(next.vals, 0, (size_t)next.cap * sizeof *next.vals);
    for (uint32_t i = 0; i < sources.cap; i++)
        if (sources.vals[i]) nvn_hm_put(&next, sources.keys + (size_t)i * sources.klen, sources.vals[i]);
    free(sources.keys);
    free(sources.vals);
    sources = next;
    return 1;
}

size_t nvn_program_source_pack(prog_t *program, size_t original_bytes) {
    if (!source_enabled() || !program || !program->owned_control || program->nstage != 1 ||
        !gfx_shader_is_nvn2(program->sd[0].control)) return 0;
    uint32_t control_bytes, code_bytes;
    if (!gfx_shader_nvn2_sizes(program->sd[0].control, &control_bytes, &code_bytes) || code_bytes < 2048) return 0;
    size_t aligned_control = ((size_t)control_bytes + 7) & ~(size_t)7;
    if (program->sd[0].control != program->owned_control ||
        program->sd[0].data != (uintptr_t)program->owned_control + aligned_control ||
        original_bytes < aligned_control + code_bytes) return 0;
    const uint8_t *code = (const uint8_t *)(uintptr_t)program->sd[0].data;
    size_t bound = ZSTD_compressBound(code_bytes);
    uint8_t *scratch = malloc(bound);
    if (!scratch) return 0;
    ZSTD_CCtx *context = ZSTD_createCCtx();
    if (!context) { free(scratch); return 0; }
    size_t a = ZSTD_CCtx_setParameter(context, ZSTD_c_compressionLevel, 1);
    size_t b = ZSTD_CCtx_setParameter(context, ZSTD_c_checksumFlag, 1);
    size_t packed_bytes = ZSTD_isError(a) || ZSTD_isError(b) ? (size_t)-1 :
        ZSTD_compress2(context, scratch, bound, code, code_bytes);
    ZSTD_freeCCtx(context);
    if (ZSTD_isError(packed_bytes) || packed_bytes + SOURCE_PREFIX + sizeof(ProgramSource) + 1024 >= code_bytes) {
        free(scratch);
        return 0;
    }
    uint8_t *packed = malloc(packed_bytes);
    if (!packed) { free(scratch); return 0; }
    memcpy(packed, scratch, packed_bytes);
    free(scratch);
    size_t fixed_bytes = aligned_control + sizeof(ProgramSource) + SOURCE_PREFIX;
    uint8_t *owned = malloc(fixed_bytes);
    if (!owned) { free(packed); return 0; }
    memcpy(owned, program->sd[0].control, control_bytes);
    ProgramSource *entry = (ProgramSource *)(void *)(owned + aligned_control);
    *entry = (ProgramSource){.raw_bytes = code_bytes, .packed_bytes = (uint32_t)packed_bytes,
        .original_bytes = original_bytes, .fixed_bytes = fixed_bytes, .packed = packed};
    uint8_t *prefix = (uint8_t *)(entry + 1);
    memcpy(prefix, code, SOURCE_PREFIX);
    uintptr_t key = (uintptr_t)prefix;
    plat_mutex_lock(&source_lock);
    if (!source_reserve()) {
        plat_mutex_unlock(&source_lock);
        free(packed);
        free(owned);
        return 0;
    }
    nvn_hm_put(&sources, &key, entry);
    source_memory.blocks++;
    source_memory.original_bytes += original_bytes;
    source_memory.retained_bytes += fixed_bytes + packed_bytes;
    source_memory.auxiliary_bytes += packed_bytes;
    plat_mutex_unlock(&source_lock);
    free(program->owned_control);
    program->owned_control = owned;
    program->sd[0] = (NVNshaderData){(uintptr_t)prefix, owned};
    return fixed_bytes;
}

const uint8_t *nvn_program_source(const uint8_t *source) {
    if (!source_enabled() || !source) return source;
    uintptr_t key = (uintptr_t)source;
    plat_mutex_lock(&source_lock);
    ProgramSource *entry = nvn_hm_get(&sources, &key);
    if (!entry) { plat_mutex_unlock(&source_lock); return source; }
    if (!entry->expanded) {
        uint8_t *raw = malloc(entry->raw_bytes);
        if (!raw) {
            plat_mutex_unlock(&source_lock);
            rt_fatal("nvn: program source expansion allocation failed");
        }
        size_t result = ZSTD_decompress(raw, entry->raw_bytes, entry->packed, entry->packed_bytes);
        if (ZSTD_isError(result) || result != entry->raw_bytes) {
            free(raw);
            plat_mutex_unlock(&source_lock);
            rt_fatal("nvn: invalid compressed program source/checksum");
        }
        entry->expanded = raw;
        source_memory.retained_bytes += entry->raw_bytes;
        source_memory.retained_bytes -= entry->packed_bytes;
        source_memory.auxiliary_bytes += entry->raw_bytes;
        source_memory.auxiliary_bytes -= entry->packed_bytes;
        free(entry->packed);
        entry->packed = NULL;
        entry->packed_bytes = 0;
    }
    const uint8_t *result = entry->expanded;
    plat_mutex_unlock(&source_lock);
    return result;
}

void nvn_program_source_release(const prog_t *program) {
    if (!source_enabled() || !program || program->nstage != 1) return;
    uintptr_t key = (uintptr_t)program->sd[0].data;
    plat_mutex_lock(&source_lock);
    ProgramSource *entry = nvn_hm_del(&sources, &key);
    if (entry) {
        size_t auxiliary = entry->expanded ? entry->raw_bytes : entry->packed_bytes;
        source_memory.blocks--;
        source_memory.original_bytes -= entry->original_bytes;
        source_memory.retained_bytes -= entry->fixed_bytes + auxiliary;
        source_memory.auxiliary_bytes -= auxiliary;
        free(entry->expanded);
        free(entry->packed);
    }
    plat_mutex_unlock(&source_lock);
}

void nvn_program_source_stats(NvnProgramSourceStats *out) {
    if (!out) return;
    plat_mutex_lock_shared(&source_lock);
    *out = source_memory;
    out->index_bytes = (uint64_t)sources.cap * (sources.klen + sizeof(void *));
    plat_mutex_unlock_shared(&source_lock);
}
