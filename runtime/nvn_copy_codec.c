// The platform side of packing idle recording copies (nvn_api_cmd.c, "packing idle copies"): own pages for a copy,
// decommitting and recommitting them, the low-priority thread and the zstd codec. Kept apart so the tests that build
// nvn_api_cmd.c alone need neither zstd nor these platform calls: there its weak fallbacks never pack.
#include <stddef.h>
#include <stdint.h>
#include <zstd.h>

#include "nvn_api.h"
#include "platform/platform.h"

void *nvn_pack_pages(size_t n) { return plat_mem_alloc(n); }
void nvn_pack_pages_free(void *p, size_t n) { plat_mem_free(p, n); }
int nvn_pack_pages_drop(void *p, size_t n) { return plat_mem_decommit((uintptr_t)p, n); }
int nvn_pack_pages_restore(void *p, size_t n) { return plat_mem_commit((uintptr_t)p, n); }

static void (*scan_fn)(void);
static uint32_t scan_period;

static void pack_main(void *arg) {
    (void)arg;
    plat_thread_priority(PLAT_PRIO_LOWEST);
    for (;;) {
        plat_sleep_ms(scan_period);
        scan_fn();
    }
}

int nvn_pack_thread(void (*scan)(void), uint32_t period_ms) {
    scan_fn = scan, scan_period = period_ms;
    return plat_thread_spawn(pack_main, NULL, "nvn copy pack", 0);
}

size_t nvn_copy_pack_bound(size_t n) { return ZSTD_compressBound(n); }

size_t nvn_copy_pack(void *dst, size_t cap, const void *src, size_t n) {
    size_t z = ZSTD_compress(dst, cap, src, n, 1);
    return ZSTD_isError(z) ? 0 : z;
}

int nvn_copy_unpack(void *dst, size_t n, const void *src, size_t z) {
    size_t got = ZSTD_decompress(dst, n, src, z);
    return !ZSTD_isError(got) && got == n;
}
