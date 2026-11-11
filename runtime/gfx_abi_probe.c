// --probe-gfx-abi: every host object of nvn_obj.h fits the storage nvn.h reserves
// for its NVN type, the layouts the game reads directly (NVNtextureView, the command buffer's inline fast-path
// fields) sit where TOTK's NVN 55 code expects them, and the GPU address window lies above every guest region.
// The same facts are static assertions in nvn_obj.h; this mode prints the sizes so a change shows its margin.
// Needs no game files.
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#endif

#include "cfg.h"
#include "gfx.h"
#include "platform/platform.h"

static int failures, checks;
#define CHECK(cond, ...) do { checks++; if (!(cond)) { failures++; printf("  FAIL: " __VA_ARGS__); putchar('\n'); } } while (0)

typedef struct { const char *host, *nvn; size_t size, reserved; } Fit;
#define FIT(T, H) {#T, #H, sizeof(T), sizeof(H)}
static const Fit fits[] = {
    FIT(dev_builder_t, NVNdeviceBuilder),       FIT(dev_t_, NVNdevice),
    FIT(queue_builder_t, NVNqueueBuilder),      FIT(queue_t, NVNqueue),
    FIT(pool_builder_t, NVNmemoryPoolBuilder),  FIT(pool_t, NVNmemoryPool),
    FIT(dpool_t, NVNtexturePool),               FIT(dpool_t, NVNsamplerPool),
    FIT(tex_builder_t, NVNtextureBuilder),      FIT(tex_t, NVNtexture),
    FIT(view_t, NVNtextureView),
    FIT(samp_builder_t, NVNsamplerBuilder),     FIT(samp_t, NVNsampler),
    FIT(prog_t, NVNprogram),
    FIT(sync_t, NVNsync),
    FIT(ev_builder_t, NVNeventBuilder),         FIT(ev_t, NVNevent),
    FIT(win_builder_t, NVNwindowBuilder),       FIT(win_t, NVNwindow),
    FIT(cmd_t, NVNcommandBuffer),
    FIT(color_st, NVNcolorState),               FIT(chmask_st, NVNchannelMaskState),
    FIT(blend_st, NVNblendState),               FIT(ms_st, NVNmultisampleState),
    FIT(poly_st, NVNpolygonState),              FIT(ds_st, NVNdepthStencilState),
    FIT(va_st, NVNvertexAttribState),           FIT(vs_st, NVNvertexStreamState),
};

_Static_assert(sizeof(NVNtextureView) == 16, "NVNtextureView is 16 bytes in TOTK's NVN 55");
_Static_assert(offsetof(cmd_t, inline_cur) == 0x20 && offsetof(cmd_t, inline_end) == 0x28, "cmd_t inline fields");
_Static_assert(RT_GPU_VA_BASE >= RT_ADDRESS_LIMIT, "GPU address window overlaps the guest address space");

// Write watch: a committed heap page and an alias page, written once, are reported once.
static void check_write_watch(void) {
    const RtGuestLayout *g = mem_layout();
    uint64_t heap = 0, pages[8], n;
    CHECK(mem_set_heap_size(0x200000, &heap) == 0 && heap == g->heap.base, "SetHeapSize 2 MiB");
    mem_written_pages(heap, 0x200000, pages, 8);   // drop whatever the commit itself marked
    volatile uint8_t *p = (volatile uint8_t *)(uintptr_t)heap;
    p[0x5000] = 1;
    n = mem_written_pages(heap, 0x200000, pages, 8);
    CHECK(n == 1 && pages[0] == heap + 0x5000, "heap write: %llu pages reported (want 1 at %#llx)",
          (unsigned long long)n, (unsigned long long)(heap + 0x5000));
    printf("write-watch: heap page written once: %llu reported at +%#llx\n", (unsigned long long)n,
           n ? (unsigned long long)(pages[0] - heap) : 0ull);
    n = mem_written_pages(heap, 0x200000, pages, 8);
    CHECK(n == 0, "second call reported %llu pages (want 0)", (unsigned long long)n);
    printf("write-watch: second call: %llu reported\n", (unsigned long long)n);
    p[0x7000] = 2;
    p[0x9000] = 3;
    p[0xA000] = 4;
    n = mem_written_pages(heap, 0x200000, pages, 2);
    uint64_t m = mem_written_pages(heap, 0x200000, pages + 2, 6);
    printf("write-watch: three pages written, max 2: %llu then %llu\n", (unsigned long long)n, (unsigned long long)m);
    CHECK(n == 2 && m == 1, "max 2 reported %llu, then %llu (want 2, 1)", (unsigned long long)n, (unsigned long long)m);

    // Frozen region: read-only until the first write thaws it (the write lands and is reported); mem_thaw (a file
    // read) thaws it and holds it thawed; SetHeapSize below decommits it.
    mem_written_pages(heap, 0x200000, pages, 8);
    CHECK(mem_freeze(heap) == 1 && mem_frozen(heap), "freeze of an idle committed 2 MiB region");
    uint64_t end;
    CHECK(plat_mem_state(heap, heap + 1, &end) == PLAT_PROT_READ, "frozen region is not read-only");
    p[0x3000] = 7;
    CHECK(!mem_frozen(heap) && p[0x3000] == 7, "a write into a frozen region did not thaw it");
    n = mem_written_pages(heap, 0x200000, pages, 8);
    CHECK(n == 1 && pages[0] == heap + 0x3000, "write after a thaw: %llu pages reported (want 1)", (unsigned long long)n);
    CHECK(mem_freeze(heap) == 1, "refreeze after a write thaw");
    mem_thaw(heap + 0x6000, 0x1000);
    CHECK(!mem_frozen(heap) && mem_freeze(heap) == 0, "mem_thaw did not thaw and hold the region");
    p[0x6000] = 8;
    printf("write-watch: frozen region thawed by a write (reported once) and by mem_thaw (held)\n");

    uint64_t a = g->alias.base + 0x10000;
    CHECK(mem_map_physical(a, 0x4000) == 0, "MapPhysicalMemory");
    mem_written_pages(a, 0x4000, pages, 8);
    *(volatile uint8_t *)(uintptr_t)(a + 0x1000) = 1;
    n = mem_written_pages(a, 0x4000, pages, 8);
    CHECK(n == 1 && pages[0] == a + 0x1000, "alias write: %llu pages reported (want 1)", (unsigned long long)n);
    CHECK(mem_written_pages(a, 0x4000, pages, 8) == 0, "alias: second call reported pages");

    // A region with reserved holes freezes its committed pages only, and a write thaws them without committing a hole.
    uint64_t ra = a & ~0x1fffffull;
    CHECK(mem_freeze(ra) == 1 && mem_frozen(ra), "freeze of a 2 MiB region with reserved holes");
    CHECK(plat_mem_state(a, a + 1, &end) == PLAT_PROT_READ, "its committed pages are not read-only");
    *(volatile uint8_t *)(uintptr_t)(a + 0x2000) = 9;
    CHECK(!mem_frozen(ra) && *(volatile uint8_t *)(uintptr_t)(a + 0x2000) == 9, "a write did not thaw the region with holes");
    CHECK(plat_mem_state(a, a + 0x10000, &end) == PLAT_PROT_RW && end == a + 0x4000, "the thawed pages are not read-write");
    CHECK(plat_mem_state(a + 0x4000, a + 0x4001, &end) == PLAT_MEM_RESERVED, "a hole got committed");
    printf("write-watch: region with reserved holes frozen and thawed by a write\n");
    CHECK(mem_unmap_physical(a, 0x4000) == 0, "UnmapPhysicalMemory");
    CHECK(mem_written_pages(RT_KERNEL_BASE, 0x1000, pages, 8) == UINT64_MAX, "kernel region is not write-watched");
    CHECK(mem_set_heap_size(0, &heap) == 0, "SetHeapSize 0");
    printf("write-watch: alias page written once: reported once, then not\n");
}

#ifdef _WIN32
typedef struct { HANDLE gate; uint64_t address; uint32_t value; } DemandProbeWrite;
static DWORD WINAPI demand_probe_write(void *opaque) {
    DemandProbeWrite *w = opaque;
    if (WaitForSingleObject(w->gate, 10000) != WAIT_OBJECT_0) return 1;
    *(volatile uint32_t *)(uintptr_t)w->address = w->value;
    return 0;
}
#endif

static void check_demand_heap(void) {
    if (!cfg_bool("TOTK_LAZY_HEAP")) return;
    const RtGuestLayout *g = mem_layout();
    uint64_t heap = 0, end;
    int before = failures;
    CHECK(!mem_set_heap_size(64ull << 20, &heap), "demand heap 64 MiB");
    CHECK(plat_mem_state(heap, heap + 1, &end) == PLAT_MEM_RESERVED, "unused heap was committed eagerly");
    volatile uint32_t *value = (volatile uint32_t *)(uintptr_t)(heap + (8ull << 20));
    CHECK(*value == 0, "first demand read is zero");
    *value = 0xdecaf123;
    CHECK(plat_mem_state(heap + (32ull << 20), heap + (32ull << 20) + 1, &end) == PLAT_MEM_RESERVED,
          "access committed an unrelated heap range");
    uint32_t read = 1;
    CHECK(plat_mem_read(heap + (48ull << 20), &read, sizeof read) && !read, "kernel-mediated read prepares its source");
    CHECK(!mem_prepare_range(heap + (64ull << 20), 1) && !plat_mem_readable(heap + (64ull << 20), 1),
          "a range beyond the logical heap must stay invalid");
    uint64_t stack = g->stack.base + 0x100000;
    CHECK(!mem_map(stack, (uintptr_t)value, 0x1000), "demand heap stack map");
    CHECK(!plat_mem_read((uintptr_t)value, &read, sizeof read) &&
          plat_mem_state((uintptr_t)value, (uintptr_t)value + 1, &end) == PLAT_PROT_NONE,
          "host preparation must preserve a moved stack's source guard");
    CHECK(!mem_unmap(stack, (uintptr_t)value, 0x1000) && *value == 0xdecaf123, "demand heap stack round trip");
    *(volatile uint32_t *)(uintptr_t)(heap + (48ull << 20)) = 99;
    CHECK(!mem_set_heap_size(32ull << 20, &heap), "shrink demand heap");
    CHECK(!mem_prepare_range(heap + (48ull << 20), 4), "shrunk heap range must stay invalid");
    CHECK(!mem_set_heap_size(64ull << 20, &heap) && *(volatile uint32_t *)(uintptr_t)(heap + (48ull << 20)) == 0,
          "regrown demand heap must not expose old bytes");
#ifdef _WIN32
    HANDLE gate = CreateEvent(NULL, TRUE, FALSE, NULL), threads[8];
    DemandProbeWrite writes[8];
    CHECK(gate != NULL, "concurrent demand-fault gate");
    for (unsigned i = 0; i < 8; i++) {
        writes[i] = (DemandProbeWrite){gate, heap + (40ull << 20) + 4096 * i, 100 + i};
        threads[i] = CreateThread(NULL, 0, demand_probe_write, &writes[i], 0, NULL);
        CHECK(threads[i] != NULL, "concurrent demand-fault writer");
    }
    SetEvent(gate);
    for (unsigned i = 0; i < 8; i++) if (threads[i]) {
        DWORD code = 1;
        CHECK(WaitForSingleObject(threads[i], 10000) == WAIT_OBJECT_0 && GetExitCodeThread(threads[i], &code) && !code,
              "concurrent demand-fault completion");
        CHECK(*(volatile uint32_t *)(uintptr_t)writes[i].address == writes[i].value, "concurrent demand write preserved");
        CloseHandle(threads[i]);
    }
    CloseHandle(gate);
    HANDLE input, output;
    CHECK(CreatePipe(&input, &output, NULL, 0), "authored kernel-IO pipe");
    const uint32_t authored[4] = {11, 22, 33, 44};
    DWORD count = 0;
    CHECK(WriteFile(output, authored, sizeof authored, &count, NULL) && count == sizeof authored, "pipe source write");
    uint64_t dst = heap + (56ull << 20);
    mem_thaw(dst, sizeof authored);
    CHECK(ReadFile(input, (void *)(uintptr_t)dst, sizeof authored, &count, NULL) && count == sizeof authored &&
          !memcmp((void *)(uintptr_t)dst, authored, sizeof authored), "prepared kernel IO into demand heap");
    uint64_t direct = dst + 0x20000;
    CHECK(plat_mem_state(direct, direct + 1, &end) == PLAT_MEM_RESERVED, "direct file-read destination starts reserved");
    CHECK(WriteFile(output, authored, sizeof authored, &count, NULL) && count == sizeof authored, "direct read source");
    CHECK(plat_file_pread((PlatFile *)input, (void *)(uintptr_t)direct, sizeof authored, 0) == sizeof authored &&
          !memcmp((void *)(uintptr_t)direct, authored, sizeof authored), "platform file read prepares its destination");
    uint64_t zero_source = heap + (58ull << 20);
    CHECK(plat_mem_state(zero_source, zero_source + 1, &end) == PLAT_MEM_RESERVED, "file-write source starts reserved");
    CHECK(plat_file_pwrite((PlatFile *)output, (const void *)(uintptr_t)zero_source, sizeof authored, 0) == sizeof authored,
          "platform file write prepares its source");
    uint32_t zero_result[4] = {1, 1, 1, 1};
    CHECK(ReadFile(input, zero_result, sizeof zero_result, &count, NULL) && count == sizeof zero_result &&
          !zero_result[0] && !zero_result[1] && !zero_result[2] && !zero_result[3], "untouched logical bytes write as zero");
    CloseHandle(input); CloseHandle(output);
#endif
    CHECK(mem_prepare_range(heap + (60ull << 20), 65536), "aligned host/GPU import preparation");
    for (uint64_t at = heap + (60ull << 20); at < heap + (60ull << 20) + 65536; at += 4096)
        CHECK(plat_mem_state(at, at + 1, &end) == PLAT_PROT_RW, "all pages of the prepared import must be accessible");
    CHECK(plat_mem_state(heap + (60ull << 20) + 65536, heap + (60ull << 20) + 65537, &end) == PLAT_MEM_RESERVED,
          "host/GPU preparation committed an unrelated page");
    *(volatile uint32_t *)(uintptr_t)(heap + (60ull << 20) + 4096) = 88;
    CHECK(plat_mem_decommit(heap + (60ull << 20) + 4096, 4096), "explicit page decommit");
    read = 1;
    CHECK(plat_mem_read(heap + (60ull << 20) + 4096, &read, sizeof read) && !read,
          "decommit invalidates the backing cache before the next host read");
    CHECK(!mem_set_heap_size(0, &heap) && !mem_prepare_range(heap, 1), "released demand heap must stay invalid");
    printf("demand-heap: zeroing, guards, stack maps, shrink/regrow and prepared IO: %s\n", failures == before ? "ok" : "FAIL");
}

int probe_gfx_abi(void) {
    size_t tightest = (size_t)-1;
    const char *tight = "";
    for (size_t i = 0; i < sizeof fits / sizeof *fits; i++) {
        const Fit *f = &fits[i];
        CHECK(f->size <= f->reserved, "%s is %zu bytes, %s reserves %zu", f->host, f->size, f->nvn, f->reserved);
        if (f->reserved - f->size < tightest) tightest = f->reserved - f->size, tight = f->host;
    }
    CHECK(sizeof(NVNtextureView) == 16 && sizeof(view_t) == 16, "NVNtextureView %zu bytes, view_t %zu (want 16)",
          sizeof(NVNtextureView), sizeof(view_t));
    CHECK(offsetof(cmd_t, guest_flags) == 0x10, "cmd_t.guest_flags at %#zx (want 0x10)", offsetof(cmd_t, guest_flags));
    CHECK(offsetof(cmd_t, inline_cur) == 0x20, "cmd_t.inline_cur at %#zx (want 0x20)", offsetof(cmd_t, inline_cur));
    CHECK(offsetof(cmd_t, inline_end) == 0x28, "cmd_t.inline_end at %#zx (want 0x28)", offsetof(cmd_t, inline_end));
    CHECK(sizeof(samp_par) == 44, "samp_par is %zu bytes (want 44)", sizeof(samp_par));
    CHECK(RT_GPU_VA_BASE >= RT_ADDRESS_LIMIT && RT_GPU_VA_SIZE > 0, "GPU address window %#llx overlaps the guest space",
          RT_GPU_VA_BASE);
    check_write_watch();
    check_demand_heap();
    printf("gfx-abi: %d layouts fit (tightest %s, %zu bytes spare), cmd_t inline fields at +0x20/+0x28, "
           "NVNtextureView %zu bytes\n", (int)(sizeof fits / sizeof *fits), tight, tightest, sizeof(NVNtextureView));
    if (failures) {
        printf("probe-gfx-abi: FAILED (%d of %d checks)\n", failures, checks);
        return 1;
    }
    printf("probe-gfx-abi: passed\n");
    return 0;
}
