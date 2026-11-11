// NVN front end, command-buffer slice: command buffers, their memory, recording
// as op records (nvn_obj.h), recording copies behind command handles, and the submit-time resolution of deferred binds
// (nvn_submit_prepare, called by nvnQueueSubmitCommands in nvn_api_core.c). Ported from MacNativeGames
// games/totk/third_party/d2r_gfx/gfx/nvn/hle_nvn.c @ b75be8cf (MIT, Copyright (c) 2026 jhaizhou; origin d2r-mobile),
// lines 647-1062, 1261-1286, 1486-1760 and 1975-1990, and the *Deferred and memory-callback entry points of
// totk_nvn_ext.c in the same directory. Metal, mach, the translation thread, Apple diagnostics, the per-frame
// statistics and the long-session sweeps (copy marks in control memory, texture pin sweep) dropped.
//
// Records are written into a growable host buffer per command buffer, never into the game's command memory: games hand
// the same command memory to several command buffers recording at once (budgeted for real NVN encodings), and our
// records are larger. The game's command and control memory is accounted only. EndRecording copies the finished
// recording into host memory; the handle is that copy (a flat OP_BEGIN ... OP_END sequence) and lives until the game
// adds the same control memory again. CallCommands and CopyCommands inline the called recording, so copies hold no
// OP_CALL or OP_JUMP.
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "gfx.h"
#include "host.h"
#include "nvn_api.h"
#include "nvn_hmap.h"
#include "nvn_mode_trace.h"
#include "nvn_reclaim.h"
#include "platform/platform.h"

#define CMD_MAGIC MAGIC('C', 'M', 'D', 'B')
#define COPY_MAGIC 0x59504f43444d43ull     // "CMDCOPY": prefix of a live recording copy
#define OP_RESERVE 24                       // room always kept at the end of the recording buffer
#define MIN_COMMAND_SIZE 512                // NVN_DEVICE_INFO_COMMAND_BUFFER_MIN_COMMAND_SIZE (nvn_api_core.c devinfo)
#define MIN_CONTROL_SIZE 256                // NVN_DEVICE_INFO_COMMAND_BUFFER_MIN_CONTROL_SIZE
#define CTL_PER_RECORDING 64                // control memory one recording is reported to use
#define COPY_PAGE_SHIFT 16                  // recording copy index granularity (release_copies)

static cmd_t *cmd_of(const Cpu *c) { return arg_p(c, 0); }

// Bounded opt-in call-order evidence; recording and all guest values are unchanged.
static int cmd_rt_trace(const Cpu *c, const char *op) {
    const char *e = getenv("HWAOI_RT_TRACE");
    if (!e || strcmp(e, "1")) return 0;
    uint64_t frame = __atomic_load_n(&gfx_counters.frames, __ATOMIC_RELAXED);
    if (frame < 20 || frame > 21) return 0;
    const cmd_t *o = cmd_of(c);
    rt_log("rt-record: frame=%llu cmd=%p offset=%llu %s lr=%#llx x1=%#llx x2=%#llx x3=%#llx",
           (unsigned long long)frame, (const void *)o,
           (unsigned long long)(o && o->seg ? o->cur - o->seg : 0), op,
           (unsigned long long)c->x[30], (unsigned long long)c->x[1],
           (unsigned long long)c->x[2], (unsigned long long)c->x[3]);
    if (!strcmp(op, "ClearColor")) {
        uint64_t fp = c->x[29];
        // The observed HWAoI wrapper's caller is the command-interpreter frame.
        // This is raw evidence only; no command meaning is inferred here.
        if (fp && !(fp & 7) && plat_mem_readable(fp, 16)) {
            uint64_t parent = LD64(fp);
            if (parent >= 16 && !(parent & 7) && plat_mem_readable(parent - 16, 16)) {
                uint64_t cursor = LD64(parent - 16);
                if (cursor >= 48 && plat_mem_readable(cursor - 48, 48)) {
                    uint32_t words[12];
                    memcpy(words, (const void *)(uintptr_t)(cursor - 48), sizeof words);
                    rt_log("rt-record: cmd=%p cursor=%#llx preceding48="
                           "%08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x",
                           (const void *)o, (unsigned long long)cursor,
                           words[0], words[1], words[2], words[3], words[4], words[5],
                           words[6], words[7], words[8], words[9], words[10], words[11]);
                }
            }
        }
        for (int i = 0; i < 3 && fp && !(fp & 7) && plat_mem_readable(fp, 16); i++) {
            uint64_t next = LD64(fp), lr = LD64(fp + 8);
            rt_log("rt-record: cmd=%p clear caller[%d]=%#llx", (const void *)o, i,
                   (unsigned long long)lr);
            if (next <= fp || next - fp > (64u << 20)) break;
            fp = next;
        }
    }
    return 1;
}

// ---------- memory callbacks
// NVN calls the game's memory callback when a recording needs command or control memory and none is left. Records
// live in host memory here, so the callback is called only where the hardware certainly would: a recording starts
// without any command memory, or ends without space for its next control record. It runs on the calling guest thread through
// rt_call; whatever memory it adds is accounted. Set{Command,Control}MemoryCallbackEnabled(false) suppress it.
enum { CB_NO_COMMAND = 1, CB_NO_CONTROL = 2 };
static PlatMutex cb_lock = PLAT_MUTEX_INIT;
static nvn_hmap cb_off = {.klen = 8};       // command buffer -> disabled callback kinds | 0x100

static unsigned cb_disabled(const cmd_t *o) {
    plat_mutex_lock_shared(&cb_lock);
    uintptr_t v = (uintptr_t)nvn_hm_get(&cb_off, &o);
    plat_mutex_unlock_shared(&cb_lock);
    return (unsigned)v & 0xff;
}

static void cb_set_disabled(const cmd_t *o, unsigned kind, int disabled) {
    plat_mutex_lock(&cb_lock);
    unsigned v = (unsigned)(uintptr_t)nvn_hm_get(&cb_off, &o) & 0xff;
    v = disabled ? v | kind : v & ~kind;
    if (v) nvn_hm_put(&cb_off, &o, (void *)(uintptr_t)(v | 0x100));
    else nvn_hm_del(&cb_off, &o);
    plat_mutex_unlock(&cb_lock);
}

static void memory_event(Cpu *c, cmd_t *o, int event, uint64_t min) {
    if (!o->cb || (cb_disabled(o) & (event == NVN_COMMAND_BUFFER_MEMORY_EVENT_OUT_OF_COMMAND_MEMORY ? CB_NO_COMMAND
                                                                                                     : CB_NO_CONTROL)))
        return;
    NVN_LOG("command buffer %p: memory callback %p (%s memory, %llu bytes)", (void *)o, (void *)o->cb,
            event == NVN_COMMAND_BUFFER_MEMORY_EVENT_OUT_OF_COMMAND_MEMORY ? "command" : "control",
            (unsigned long long)min);
    rt_call(c, (uint64_t)(uintptr_t)o->cb, 4, (uint64_t)(uintptr_t)o, (uint64_t)event, min,
            (uint64_t)(uintptr_t)o->cb_data);
}

// ---------- recording buffer
static uint64_t record_buffers, record_bytes, record_peak_bytes, record_reinits;

static void put_hdr(uint8_t *p, uint32_t op, uint32_t size) {
    op_hdr h = {op, size};
    memcpy(p, &h, sizeof h);
}

static void cmd_reserve(cmd_t *o, size_t need) {
    if (o->seg && o->cur + need + OP_RESERVE <= o->end) return;
    size_t used = o->seg ? (size_t)(o->cur - o->seg) : 0;
    size_t cap = o->seg ? (size_t)(o->end - o->seg) : 0;
    size_t previous_cap = cap;
    int first_allocation = o->seg == NULL;
    if (!cap) cap = 64 << 10;
    while (used + need + OP_RESERVE > cap) cap *= 2;
    uint8_t *b = realloc(o->seg, cap);
    // A block this size fails only without commit (the fatal report states it) or when seg is no block of ours: a
    // command buffer object the game overwrote or copied shows a wrong magic or more used than its old size.
    if (!b)
        rt_fatal("nvn: command recording buffer of %zu bytes (command buffer %p, magic %#x, buffer %p of %zu bytes, %zu "
                 "used, %zu asked)", cap, (void *)o, o->magic, (void *)o->seg, o->seg ? (size_t)(o->end - o->seg) : 0,
                 used, need);
    if (first_allocation) __atomic_fetch_add(&record_buffers, 1, __ATOMIC_RELAXED);
    uint64_t total = __atomic_add_fetch(&record_bytes, cap - previous_cap, __ATOMIC_RELAXED);
    uint64_t peak = __atomic_load_n(&record_peak_bytes, __ATOMIC_RELAXED);
    while (total > peak && !__atomic_compare_exchange_n(&record_peak_bytes, &peak, total, 0,
                                                        __ATOMIC_RELAXED, __ATOMIC_RELAXED)) {}
    o->seg = b;
    o->cur = b + used;
    o->end = b + cap;
}

// Space for one record (header included); the header's op is filled by the caller.
static uint8_t *cmd_alloc(cmd_t *o, uint32_t size) {
    size = (size + 7) & ~7u;
    cmd_reserve(o, size);
    uint8_t *p = o->cur;
    o->cur += size;
    o->cmd_used += size;
    o->hw_used += size;
    o->last_rec = size;
    put_hdr(p, 0, size);
    return p;
}

// The game sums nvnCommandBufferGetCommandMemoryUsed over recordings to size resource heaps, so usage must be what the
// real NVN push-buffer encoding takes, not our record size. Known encodings (Maxwell methods, from a real recording):
// DrawElements 12 words, DrawElementsBaseVertex 11, BindVertexAttribState 1+16, BindVertexStreamState 16+n,
// BindVertexBuffer 6 words each.
static void hw_fix(cmd_t *o, size_t bytes) {
    o->hw_used += bytes;
    o->hw_used -= o->last_rec;
    o->last_rec = bytes;
}

// Mode 2 targets the SDK's copy engine. These already implemented host records
// have mode-independent copy arguments or conservatively broader queue ordering.
// Everything else is refused, including commands copied from ordinary recordings.
static void recording_mode_check(const cmd_t *o, uint32_t op) {
    if (o->recording_mode != 2) return;
    switch (op) {
    case OP_BEGIN: case OP_END:
    case OP_COPY_B2B: case OP_COPY_B2T: case OP_COPY_T2B: case OP_COPY_T2T: case OP_COPY_STRIDE: case OP_COPY_IMAGE_STRIDE:
    case OP_BARRIER: case OP_WAIT_SYNC: case OP_FENCE_SYNC:
        return;
    default:
        rt_fatal("nvn: copy recording mode 2 does not support recorded operation %u", op);
    }
}

// A zeroed payload of one record.
static void *rec(cmd_t *o, uint32_t op, size_t payload) {
    recording_mode_check(o, op);
    uint32_t sz = (uint32_t)((8 + payload + 7) & ~(size_t)7);
    uint8_t *p = cmd_alloc(o, sz);
    put_hdr(p, op, sz);
    memset(p + 8, 0, sz - 8);
    return p + 8;
}
#define REC(o, OP, T) ((T *)rec((o), (OP), sizeof(T)))
// Array-carrying binds take the prefix fields plus n elements; the replay reads only the first n.
#define RECN(o, OP, T, ARR, N) ((T *)rec((o), (OP), offsetof(T, ARR) + sizeof(((T *)0)->ARR[0]) * (size_t)(N)))

static void rec_array(cmd_t *o, uint32_t op, int first, int n, const void *source, size_t item_size) {
    recording_mode_check(o, op);
    uint8_t values[16 * sizeof(NVNbufferRange)];
    if (n < 0 || n > 16 || item_size > sizeof(NVNbufferRange)) rt_fatal("nvn: invalid host array record");
    if (n) memcpy(values, source, (size_t)n * item_size);
    size_t size = nvn_array_pack(NULL, first, n, values, item_size);
    if (!size) rt_fatal("nvn: invalid host array range");
    nvn_array_pack(rec(o, op, size), first, n, values, item_size);
}

static void copy_view(view_t *dst, uint8_t *has, const NVNtextureView *v) {
    *has = v != NULL;
    if (v) *dst = *(const view_t *)v;
}

// ---------- texture pins
// Recordings refer to textures by pointer, but the game may finalize or reuse an NVNtexture after recording and before
// the submit (NVN itself captured only GPU state). Records therefore point at an immutable host copy per texture
// initialization (tex_t.serial). The copies are kept for the whole run (MacNativeGames sweeps them for long sessions).
static PlatMutex pin_lock = PLAT_MUTEX_INIT;
static nvn_hmap pins = {.klen = 8};         // serial -> tex_t copy

static const tex_t *tex_pin(const NVNtexture *nt) {
    const tex_t *t = (const tex_t *)nt;
    if (!t) return NULL;
    uint64_t serial = t->serial;
    if (!serial) rt_fatal("nvn: a recording references the uninitialized texture object %p", (const void *)t);
    plat_mutex_lock_shared(&pin_lock);
    const tex_t *r = nvn_hm_get(&pins, &serial);
    plat_mutex_unlock_shared(&pin_lock);
    if (r) return r;
    plat_mutex_lock(&pin_lock);
    r = nvn_hm_get(&pins, &serial);
    if (!r) {
        tex_t *copy = malloc(sizeof *copy);
        if (!copy) rt_fatal("nvn: texture pin copy");
        *copy = *t;
        nvn_hm_put(&pins, &serial, copy);
        r = copy;
    }
    plat_mutex_unlock(&pin_lock);
    return r;
}

// ---------- recording copies
// A handle points just past a copy_prefix. Copies are indexed by the 64 KiB block of their control-memory position, so
// adding control memory again releases the copies recorded there without walking every live copy. 4 KiB pages cost
// about 500 lookups per AddControlMemory under the exclusive lock (1.4.3 field: 53 calls a frame, 2 MiB ranges, 3.7
// copies per page), and the recording threads waited on it.
typedef struct { uint64_t magic, bytes, made_at, used_at; uint32_t deferred, sync_ops; } copy_prefix; // deferred: 0 unknown, 1 none, 2 some
_Static_assert(sizeof(copy_prefix) % 8 == 0, "records stay 8-byte aligned");
enum { COPY_SYNC_KNOWN = 1, COPY_SYNC_WAIT = 2, COPY_SYNC_FENCE = 4 };
typedef struct { uintptr_t lo, hi; uint8_t *copy; } cmd_copy;
typedef struct { cmd_copy *v; uint32_t n, cap; } copy_bucket;
static PlatMutex copy_lock = PLAT_MUTEX_INIT;    // guards the two maps, the counters and every prefix
static nvn_hmap copy_pages = {.klen = 8};   // page -> copy_bucket
static nvn_hmap copy_live = {.klen = 8};    // handle -> copy_prefix of a live copy
static uint64_t ncopies, copy_bytes, copies_made, nsubmits;
static uint64_t copy_bucket_bytes, zombie_bytes;
// Asynchronous replay: copies released by the game, and per-submission clones, that a queued submission still reads.
// Freed once gfx_replayed_submits() reaches their used_at. Empty with synchronous replay.
static copy_prefix **zombies;
static uint32_t nzombies, cap_zombies;

static void zombie_add(copy_prefix *pf) {   // copy_lock held
    if (nzombies == cap_zombies) {
        cap_zombies = cap_zombies ? cap_zombies * 2 : 64;
        zombies = realloc(zombies, sizeof *zombies * cap_zombies);
        if (!zombies) rt_fatal("nvn: out of memory (released recording copies)");
    }
    zombies[nzombies++] = pf;
    zombie_bytes += sizeof *pf + pf->bytes;
}

static void zombie_sweep(uint64_t replayed) {   // copy_lock held
    for (uint32_t i = 0; i < nzombies;) {
        if (zombies[i]->used_at <= replayed) {
            zombie_bytes -= sizeof *zombies[i] + zombies[i]->bytes;
            free(zombies[i]);
            zombies[i] = zombies[--nzombies];
        } else {
            i++;
        }
    }
}

static copy_prefix *copy_find(uint64_t handle) {   // copy_lock held
    return nvn_hm_get(&copy_live, &handle);
}

int nvn_handle_live(uint64_t handle) {
    plat_mutex_lock_shared(&copy_lock);
    int live = copy_find(handle) != NULL;
    plat_mutex_unlock_shared(&copy_lock);
    return live;
}

static uint64_t copy_info(uint64_t handle, uint32_t *sync_ops) {
    plat_mutex_lock_shared(&copy_lock);
    const copy_prefix *pf = copy_find(handle);
    uint64_t n = pf ? pf->bytes : 0;
    if (sync_ops) *sync_ops = pf ? pf->sync_ops : 0;
    plat_mutex_unlock_shared(&copy_lock);
    return n;
}
uint64_t nvn_handle_bytes(uint64_t handle) { return copy_info(handle, NULL); }

// Marks a live copy used by submission now; its size from OP_BEGIN to OP_END inclusive.
static uint64_t copy_touch(uint64_t handle, uint64_t now, const char *what) {
    plat_mutex_lock_shared(&copy_lock);
    copy_prefix *pf = copy_find(handle);
    uint64_t bytes = 0;
    if (pf) __atomic_store_n(&pf->used_at, now, __ATOMIC_RELAXED), bytes = pf->bytes;
    plat_mutex_unlock_shared(&copy_lock);
    if (!pf) rt_fatal("nvn: %s: command handle %#llx is not a live recording (released or never recorded)", what,
                      (unsigned long long)handle);
    return bytes;
}

// The recording [p, p + n), OP_BEGIN to OP_END (the recording buffer is one block: recording emits no OP_JUMP), copied
// into one host block of its exact size. One allocation and one copy: growing the copy record by record cost the
// game's main thread about 1 ms a frame in reallocations and small copies (1.4.3, field).
static uint8_t *flatten_recording(const uint8_t *p, size_t n) {
    uint8_t *out = malloc(sizeof(copy_prefix) + n);
    if (!out) rt_fatal("nvn: command copy allocation");
    memcpy(out + sizeof(copy_prefix), p, n);
    uint32_t deferred = 1, sync_ops = COPY_SYNC_KNOWN;
    for (size_t at = 0;;) {   // the records stay checked: a corrupt one stops here, not in the replay
        op_hdr h;
        memcpy(&h, p + at, sizeof h);
        if (h.size < 8 || (h.size & 7) || h.op >= OP_COUNT_ || h.op == OP_JUMP || h.op == OP_CALL || at + h.size > n ||
            (!at && h.op != OP_BEGIN))
            rt_fatal("nvn: corrupt recording at %p (op %u size %u)", (const void *)(p + at), h.op, h.size);
        if (h.op == OP_WAIT_SYNC) sync_ops |= COPY_SYNC_WAIT;
        if (h.op == OP_FENCE_SYNC) sync_ops |= COPY_SYNC_FENCE;
        if (h.op == OP_BIND_TEX || h.op == OP_BIND_SAMP || h.op == OP_BIND_IMG) {
            NvnOpScratch normalized;
            const p_bind_handles *b = nvn_op_normalize((const op_hdr *)(const void *)(p + at), &normalized);
            if (!b) rt_fatal("nvn: invalid handle bind while flattening a recording");
            if (b->src && b->n) deferred = 2;
        } else if (h.op == OP_PUSH_CONST) {
            if (h.size < 8 + offsetof(p_push_const, w)) rt_fatal("nvn: short push-constant recording");
            const p_push_const *b = (const p_push_const *)(const void *)(p + at + 8);
            if (b->src) deferred = 2;
        }
        at += h.size;
        if (h.op == OP_END) {
            if (at != n) rt_fatal("nvn: recording at %p ends at %zu of %zu bytes", (const void *)p, at, n);
            break;
        }
    }
    // Structure and source pointers are immutable after flattening; only the
    // data in deferred payloads changes. Reuse these facts instead of walking
    // an immediate recording three more times at every submission.
    *(copy_prefix *)out = (copy_prefix){.magic = COPY_MAGIC, .bytes = n, .deferred = deferred, .sync_ops = sync_ops};
    return out + sizeof(copy_prefix);
}

static void copy_add(uintptr_t lo, uintptr_t hi, uint8_t *copy) {
    copy_prefix *pf = (copy_prefix *)copy - 1;
    uint64_t handle = (uintptr_t)copy;
    uintptr_t page = lo >> COPY_PAGE_SHIFT;
    plat_mutex_lock(&copy_lock);
    pf->made_at = nsubmits;
    copy_bucket *b = nvn_hm_get(&copy_pages, &page);
    if (!b) {
        b = calloc(1, sizeof *b);
        if (!b) rt_fatal("nvn: command copy index");
        nvn_hm_put(&copy_pages, &page, b);
        copy_bucket_bytes += sizeof *b;
    }
    if (b->n == b->cap) {
        uint32_t old_cap = b->cap;
        b->cap = b->cap ? b->cap * 2 : 8;
        b->v = realloc(b->v, b->cap * sizeof *b->v);
        if (!b->v) rt_fatal("nvn: command copy index");
        copy_bucket_bytes += (uint64_t)(b->cap - old_cap) * sizeof *b->v;
    }
    b->v[b->n++] = (cmd_copy){lo, hi, copy};
    nvn_hm_put(&copy_live, &handle, pf);
    ncopies++;
    copy_bytes += pf->bytes;
    if (++copies_made % 20000 == 0)
        NVN_LOG("recording copies: %llu made, %llu live, %.1f MiB, submit %llu", (unsigned long long)copies_made,
                (unsigned long long)ncopies, copy_bytes / 1048576.0, (unsigned long long)nsubmits);
    plat_mutex_unlock(&copy_lock);
}

// Adding control memory again retires every recording copy made in it (the driver's handle lived there).
static void copy_garbage_free(void *head) {
    copy_prefix *entry = head;
    while (entry) {
        copy_prefix *next;
        memcpy(&next, (const uint8_t *)(entry + 1), sizeof next);
        free(entry);
        entry = next;
    }
}

static void release_copies(uintptr_t lo, uintptr_t hi) {
    if (hi <= lo) return;
    int diag = nvn_memory_diagnostics();
    uint64_t begin = diag ? plat_now_ns() : 0, released = 0, released_bytes = 0;
    uint64_t replayed = gfx_replayed_submits();
    copy_prefix *garbage = NULL;
    uint64_t garbage_count = 0, garbage_bytes = 0;
    plat_mutex_lock(&copy_lock);
    uint64_t locked = diag ? plat_now_ns() : 0;
    if (nzombies) zombie_sweep(replayed);
    // The index holds a record in its starting page only. A fixed-size control record can straddle the preceding
    // page, so inspect that page too when the new storage overlaps the tail of such a record.
    uintptr_t first = lo > CTL_PER_RECORDING - 1 ? lo - (CTL_PER_RECORDING - 1) : 0;
    for (uintptr_t pg = first >> COPY_PAGE_SHIFT; pg <= (hi - 1) >> COPY_PAGE_SHIFT; pg++) {
        copy_bucket *b = nvn_hm_get(&copy_pages, &pg);
        if (!b) continue;
        for (uint32_t i = 0; i < b->n;) {
            cmd_copy *k = &b->v[i];
            if (!(k->lo < hi && lo < k->hi)) { i++; continue; }
            uint64_t handle = (uintptr_t)k->copy;
            copy_prefix *pf = (copy_prefix *)k->copy - 1;
            nvn_hm_del(&copy_live, &handle);
            ncopies--;
            copy_bytes -= pf->bytes;
            pf->magic = 0;
            released++;
            released_bytes += sizeof *pf + pf->bytes;
            if (pf->used_at > replayed) zombie_add(pf);   // a queued submission still reads it
            else {
                // Both public and replay lifetimes ended. Its retired payload
                // can hold a free-list link; pending/zombie payloads stay intact.
                memcpy((uint8_t *)(pf + 1), &garbage, sizeof garbage);
                garbage = pf;
                garbage_count++;
                garbage_bytes += sizeof *pf + pf->bytes;
            }
            b->v[i] = b->v[--b->n];
        }
        if (!b->n) {
            nvn_hm_del(&copy_pages, &pg);
            copy_bucket_bytes -= sizeof *b + (uint64_t)b->cap * sizeof *b->v;
            free(b->v);
            free(b);
        }
    }
    uint64_t done = diag ? plat_now_ns() : 0;
    plat_mutex_unlock(&copy_lock);
    int queued = 0;
    if (garbage) {
        if (garbage_count >= 1024) queued = nvn_reclaim_submit(garbage, garbage_count, garbage_bytes, copy_garbage_free);
        if (!queued) copy_garbage_free(garbage);
    }
    uint64_t finished = diag ? plat_now_ns() : 0;
    if (diag && released >= 1024)
        rt_log("copy-retire: copies=%llu bytes_mib=%.1f wait_ms=%.3f locked_ms=%.3f total_ms=%.3f reclaimer=%s",
               (unsigned long long)released, released_bytes / 1048576.0,
               (locked - begin) / 1e6, (done - locked) / 1e6, (finished - begin) / 1e6, queued ? "worker" : "caller");
}

// ---------- CommandBuffer: object and memory
// NVNboolean nvnCommandBufferInitialize(NVNcommandBuffer *cmd, NVNdevice *device): the inline fast-path fields are kept
// at ~0 / 0 so NVN 55 inline code never finds room and calls the API (nvn_obj.h cmd_t)
NVN_API(nvnCommandBufferInitialize) {
    cmd_t *o = cmd_of(c);
    // Initialize over an object that was never finalized drops its recording buffer (seg lives in host memory). Logged
    // only: the old bytes may be a copy whose buffer another object owns, so freeing it here is not safe.
    static uint32_t reinits;
    if (o->magic == CMD_MAGIC && o->seg) {
        __atomic_fetch_add(&record_reinits, 1, __ATOMIC_RELAXED);
        if (__atomic_fetch_add(&reinits, 1, __ATOMIC_RELAXED) < 8)
            rt_log("nvn: Initialize over the command buffer %p, which was not finalized; its %zu-byte recording buffer %p "
                   "is dropped", (void *)o, (size_t)(o->end - o->seg), (void *)o->seg);
    }
    memset(o, 0, sizeof *o);
    o->magic = CMD_MAGIC;
    o->dev = arg_p(c, 1);
    o->inline_cur = ~0ull;
    o->inline_end = 0;
    cb_set_disabled(o, CB_NO_COMMAND | CB_NO_CONTROL, 0);
    nvn_mode_trace(NVN_MODE_INIT, (uintptr_t)o, 0, 0, 0);
    ret_x(c, 1);
}
// void nvnCommandBufferFinalize(NVNcommandBuffer *cmd): handles outlive their recorder
NVN_API(nvnCommandBufferFinalize) {
    cmd_t *o = cmd_of(c);
    if (o->seg) {
        __atomic_fetch_sub(&record_bytes, (size_t)(o->end - o->seg), __ATOMIC_RELAXED);
        __atomic_fetch_sub(&record_buffers, 1, __ATOMIC_RELAXED);
    }
    free(o->seg);
    o->seg = o->cur = o->end = NULL;
    o->magic = 0;
    cb_set_disabled(o, CB_NO_COMMAND | CB_NO_CONTROL, 0);
    nvn_mode_trace(NVN_MODE_FINALIZE, (uintptr_t)o, 0, 0, 0);
}
// void nvnCommandBufferSetMemoryCallback(NVNcommandBuffer *cmd, PFNNVNCOMMANDBUFFERMEMORYCALLBACKPROC callback)
NVN_API(nvnCommandBufferSetMemoryCallback) { cmd_of(c)->cb = arg_p(c, 1); }
// void nvnCommandBufferSetMemoryCallbackData(NVNcommandBuffer *cmd, void *data)
NVN_API(nvnCommandBufferSetMemoryCallbackData) { cmd_of(c)->cb_data = arg_p(c, 1); }
// PFNNVNCOMMANDBUFFERMEMORYCALLBACKPROC nvnCommandBufferGetMemoryCallback(const NVNcommandBuffer *cmd)
NVN_API(nvnCommandBufferGetMemoryCallback) { ret_x(c, (uintptr_t)cmd_of(c)->cb); }
// void *nvnCommandBufferGetMemoryCallbackData(const NVNcommandBuffer *cmd) (nvn.h declares it void)
NVN_API(nvnCommandBufferGetMemoryCallbackData) { ret_x(c, (uintptr_t)cmd_of(c)->cb_data); }
// void nvnCommandBufferSetCommandMemoryCallbackEnabled / SetControlMemoryCallbackEnabled(NVNcommandBuffer *cmd,
// NVNboolean enabled) (NVN 55, no prototype in nvn.h)
NVN_API(nvnCommandBufferSetCommandMemoryCallbackEnabled) { cb_set_disabled(cmd_of(c), CB_NO_COMMAND, !nvn_arg_bool(c, 1)); }
NVN_API(nvnCommandBufferSetControlMemoryCallbackEnabled) { cb_set_disabled(cmd_of(c), CB_NO_CONTROL, !nvn_arg_bool(c, 1)); }
// void nvnCommandBufferAddCommandMemory(NVNcommandBuffer *cmd, const NVNmemoryPool *pool, ptrdiff_t offset, size_t size):
// accounting only; command memory can be shared by independent recordings with distinct control memory
NVN_API(nvnCommandBufferAddCommandMemory) {
    cmd_t *o = cmd_of(c);
    const pool_t *p = arg_p(c, 1);
    o->cmd_base = (uintptr_t)(p ? p->mem : NULL) + (uintptr_t)arg_x(c, 2);
    o->game_size = arg_x(c, 3);
    o->cmd_used = 0;
    o->hw_used = 0;
}
// void nvnCommandBufferAddControlMemory(NVNcommandBuffer *cmd, void *memory, size_t size): the SDK encodes handles in
// control memory (sdk+0x444530), so reusing that storage retires the copies recorded there
NVN_API(nvnCommandBufferAddControlMemory) {
    cmd_t *o = cmd_of(c);
    void *m = arg_p(c, 1);
    size_t n = arg_x(c, 2);
    if (n && (!m || (uintptr_t)m > UINTPTR_MAX - n))
        rt_fatal("nvn: AddControlMemory requires a nonnull, non-wrapping storage range (%p, %zu bytes)", m, n);
    release_copies((uintptr_t)m, (uintptr_t)m + n);
    o->ctl = m;
    o->ctl_size = n;
    o->ctl_used = 0;
}
// size_t nvnCommandBufferGet{Control,Command}Memory{Size,Used,Free}(const NVNcommandBuffer *cmd): usage is bounded by
// the memory the game gave, so the game never sees more used than it provided
static size_t min_size(size_t a, size_t b) { return a < b ? a : b; }
NVN_API(nvnCommandBufferGetControlMemorySize) { ret_x(c, cmd_of(c)->ctl_size); }
NVN_API(nvnCommandBufferGetControlMemoryUsed) { const cmd_t *o = cmd_of(c); ret_x(c, min_size(o->ctl_used, o->ctl_size)); }
NVN_API(nvnCommandBufferGetControlMemoryFree) { const cmd_t *o = cmd_of(c); ret_x(c, o->ctl_size - min_size(o->ctl_used, o->ctl_size)); }
NVN_API(nvnCommandBufferGetCommandMemorySize) { ret_x(c, cmd_of(c)->game_size); }
NVN_API(nvnCommandBufferGetCommandMemoryUsed) { const cmd_t *o = cmd_of(c); ret_x(c, min_size(o->hw_used, o->game_size)); }
NVN_API(nvnCommandBufferGetCommandMemoryFree) { const cmd_t *o = cmd_of(c); ret_x(c, o->game_size - min_size(o->hw_used, o->game_size)); }

// ---------- CommandBuffer: recording
// void nvnCommandBufferBeginRecording(NVNcommandBuffer *cmd)
NVN_API(nvnCommandBufferBeginRecording) {
    cmd_t *o = cmd_of(c);
    if (o->recording) rt_log("nvn: BeginRecording on command buffer %p while it records", (void *)o);
    o->recording = 1;
    o->cur = o->seg;   // the previous recording was copied at EndRecording
    o->rec_start = o->cmd_used;
    put_hdr(cmd_alloc(o, 8), OP_BEGIN, 8);
    o->hw_used -= 8;   // BEGIN and END have no hardware equivalent
    o->hw_start = o->hw_used;
    nvn_mode_trace(NVN_MODE_BEGIN, (uintptr_t)o, 0, 0, 0);
    if (!o->game_size) memory_event(c, o, NVN_COMMAND_BUFFER_MEMORY_EVENT_OUT_OF_COMMAND_MEMORY, MIN_COMMAND_SIZE);
}
// NVNcommandHandle nvnCommandBufferEndRecording(NVNcommandBuffer *cmd)
NVN_API(nvnCommandBufferEndRecording) {
    cmd_t *o = cmd_of(c);
    if (!o->recording || !o->seg) rt_fatal("nvn: EndRecording on command buffer %p, which does not record", (void *)o);
    // Checking only ctl_size misses exhaustion after its first use. The former fallback then assigned every new
    // handle to the same first byte without a callback or retirement; those host copies accumulated indefinitely.
    if (!o->ctl || o->ctl_used > o->ctl_size || CTL_PER_RECORDING > o->ctl_size - o->ctl_used)
        memory_event(c, o, NVN_COMMAND_BUFFER_MEMORY_EVENT_OUT_OF_CONTROL_MEMORY, MIN_CONTROL_SIZE);
    if (!o->ctl || o->ctl_used > o->ctl_size || CTL_PER_RECORDING > o->ctl_size - o->ctl_used ||
        (uintptr_t)o->ctl > UINTPTR_MAX - o->ctl_used ||
        (uintptr_t)o->ctl + o->ctl_used > UINTPTR_MAX - CTL_PER_RECORDING)
        rt_fatal("nvn: EndRecording has no space for its %u-byte control record after the memory callback "
                 "(command buffer %p, control %p, size %zu, used %zu)",
                 CTL_PER_RECORDING, (void *)o, o->ctl, o->ctl_size, o->ctl_used);
    put_hdr(cmd_alloc(o, 8), OP_END, 8);
    o->hw_used -= 8;
    o->recording = 0;
    uint8_t *copy = flatten_recording(o->seg, (size_t)(o->cur - o->seg));
    // Finalizing the recorder does not retire its handle; only reusing the control memory it was recorded in does.
    uintptr_t lo = (uintptr_t)o->ctl + o->ctl_used;
    uintptr_t hi = lo + CTL_PER_RECORDING;
    o->ctl_used += CTL_PER_RECORDING;
    copy_add(lo, hi, copy);
    nvn_mode_trace(NVN_MODE_END, (uintptr_t)o, (uintptr_t)copy, 0, 0);
    ret_x(c, (uintptr_t)copy);
}
// NVNboolean nvnCommandBufferIsRecording(const NVNcommandBuffer *cmd)
NVN_API(nvnCommandBufferIsRecording) { ret_x(c, cmd_of(c)->recording != 0); }

// The called recording is copied into the caller at the call position: the game recycles command memory in small steps,
// so a handle may be gone by the time the caller is submitted. Deferred binds stay deferred. A copy is OP_BEGIN, its
// records and OP_END (flatten_recording checked them), so the records go over as one block; usage grows by the same
// bytes as record by record.
static void inline_recording(cmd_t *o, uint64_t handle, const char *what) {
    if (!handle) rt_fatal("nvn: %s with a null handle", what);
    uint64_t bytes = copy_touch(handle, __atomic_load_n(&nsubmits, __ATOMIC_RELAXED), what);
    if (bytes <= 16) return;
    size_t body = (size_t)bytes - 16;
    if (o->recording_mode == 2) {
        const uint8_t *p = (const uint8_t *)(uintptr_t)handle + 8;
        const uint8_t *end = p + body;
        while (p < end) {
            op_hdr h;
            memcpy(&h, p, sizeof h);
            if (h.size < sizeof h || (h.size & 7) || h.size > (size_t)(end - p))
                rt_fatal("nvn: corrupt %s recording while checking copy mode", what);
            recording_mode_check(o, h.op);
            p += h.size;
        }
    }
    cmd_reserve(o, body);
    memcpy(o->cur, (const uint8_t *)(uintptr_t)handle + 8, body);
    o->cur += body;
    o->cmd_used += body;
    o->hw_used += body;
    o->last_rec = 0;
}
// void nvnCommandBuffer{Call,Copy}Commands(NVNcommandBuffer *cmd, int n, const NVNcommandHandle *handles)
NVN_API(nvnCommandBufferCallCommands) {
    cmd_rt_trace(c, "CallCommands");
    cmd_t *o = cmd_of(c);
    int n = (int)arg_x(c, 1);
    const uint64_t *h = arg_p(c, 2);
    for (int i = 0; i < n; i++) inline_recording(o, h[i], "CallCommands");
}
NVN_API(nvnCommandBufferCopyCommands) {
    cmd_rt_trace(c, "CopyCommands");
    cmd_t *o = cmd_of(c);
    int n = (int)arg_x(c, 1);
    const uint64_t *h = arg_p(c, 2);
    for (int i = 0; i < n; i++) inline_recording(o, h[i], "CopyCommands");
}

// ---------- CommandBuffer: commands with CPU-visible results (performed by the back end in GPU order)
// void nvnCommandBufferSignalEvent(NVNcommandBuffer *cmd, const NVNevent *event, NVNeventSignalMode mode,
// NVNeventSignalLocation location, int flags, uint32_t value)
NVN_API(nvnCommandBufferSignalEvent) {
    const ev_t *e = arg_p(c, 1);
    *REC(cmd_of(c), OP_SIGNAL_EVENT, p_signal_event) = (p_signal_event){e->val, (uint32_t)arg_x(c, 2), (uint32_t)arg_x(c, 5)};
}
// void nvnCommandBufferResetCounter(NVNcommandBuffer *cmd, NVNcounterType type)
NVN_API(nvnCommandBufferResetCounter) { REC(cmd_of(c), OP_RESET_COUNTER, p_reset_counter)->type = (uint32_t)arg_x(c, 1); }
// void nvnCommandBufferReportCounter(NVNcommandBuffer *cmd, NVNcounterType type, NVNbufferAddress address)
NVN_API(nvnCommandBufferReportCounter) {
    *REC(cmd_of(c), OP_REPORT_COUNTER, p_report_counter) = (p_report_counter){(uint32_t)arg_x(c, 1), arg_x(c, 2)};
}
// void nvnCommandBufferCopyBufferToBuffer(NVNcommandBuffer *cmd, NVNbufferAddress src, NVNbufferAddress dst,
// size_t size, int flags)
NVN_API(nvnCommandBufferCopyBufferToBuffer) {
    *REC(cmd_of(c), OP_COPY_B2B, p_copy_b2b) = (p_copy_b2b){arg_x(c, 1), arg_x(c, 2), arg_x(c, 3)};
}
// void nvnCommandBufferUpdateUniformBuffer(NVNcommandBuffer *cmd, NVNbufferAddress buffer, size_t buffer_size,
// ptrdiff_t offset, size_t size, const void *data): the data is copied at record time (the caller may change it
// afterwards) and written into the buffer at replay
NVN_API(nvnCommandBufferUpdateUniformBuffer) {
    uint64_t dst = arg_x(c, 1) + arg_x(c, 3), n = arg_x(c, 4);
    const void *data = arg_p(c, 5);
    if (n > (16u << 20)) rt_fatal("nvn: UpdateUniformBuffer of %#llx bytes", (unsigned long long)n);
    p_update_ub *r = rec(cmd_of(c), OP_UPDATE_UB, offsetof(p_update_ub, data) + n);
    r->dst = dst;
    r->size = n;
    memcpy(r->data, data, n);
}

// ---------- CommandBuffer: render state, payloads of nvn_obj.h
// void nvnCommandBufferSetRenderTargets(NVNcommandBuffer *cmd, int n, const NVNtexture *const *colors,
// const NVNtextureView *const *color_views, const NVNtexture *depth, const NVNtextureView *depth_view)
NVN_API(nvnCommandBufferSetRenderTargets) {
    int n = (int)arg_x(c, 1);
    const NVNtexture *const *ct = arg_p(c, 2);
    const NVNtextureView *const *cv = arg_p(c, 3);
    const NVNtexture *dt = arg_p(c, 4);
    const NVNtextureView *dv = arg_p(c, 5);
    if (n < 0 || n > 8) {
        rt_log("nvn: SetRenderTargets with %d colour targets", n);
        n = n < 0 ? 0 : 8;
    }
    cmd_t *o = cmd_of(c);
    recording_mode_check(o, OP_SET_RT);
    p_set_rt value = {0}, *r = &value;
    r->n = n;
    for (int i = 0; i < n; i++) {
        r->color[i] = ct ? tex_pin(ct[i]) : NULL;
        copy_view(&r->cview[i], &r->has_cview[i], cv ? cv[i] : NULL);
    }
    r->depth = tex_pin(dt);
    copy_view(&r->dview, &r->has_dview, dv);
    nvn_rt_pack(rec(o, OP_SET_RT, nvn_rt_pack(NULL, r)), r);
    hw_fix(o, (8 + sizeof *r + 7) & ~(size_t)7); // preserve guest-visible command-memory usage
    if (cmd_rt_trace(c, "SetRenderTargets")) {
        for (int i = 0; i < n; i++)
            rt_log("rt-record: cmd=%p color[%d] object=%p storage=%p serial=%llu",
                   (void *)cmd_of(c), i, ct ? (const void *)ct[i] : NULL,
                   r->color[i] ? (const void *)r->color[i]->mem : NULL,
                   (unsigned long long)(r->color[i] ? r->color[i]->serial : 0));
        rt_log("rt-record: cmd=%p depth object=%p storage=%p", (void *)cmd_of(c),
               (const void *)dt, r->depth ? (const void *)r->depth->mem : NULL);
    }
}
// void nvnCommandBufferClearColor(NVNcommandBuffer *cmd, int index, const float *rgba, int mask)
NVN_API(nvnCommandBufferClearColor) {
    p_clear_color *r = REC(cmd_of(c), OP_CLEAR_COLOR, p_clear_color);
    r->index = (int)arg_x(c, 1);
    r->mask = (int)arg_x(c, 3);
    memcpy(r->rgba, arg_p(c, 2), 16);
    if (cmd_rt_trace(c, "ClearColor"))
        rt_log("rt-record: cmd=%p clear target=%d mask=%#x rgba=%g,%g,%g,%g", (void *)cmd_of(c),
               r->index, r->mask, r->rgba[0], r->rgba[1], r->rgba[2], r->rgba[3]);
}
// void nvnCommandBufferClearDepthStencil(NVNcommandBuffer *cmd, float depth, NVNboolean depth_mask, int stencil,
// int stencil_mask)
NVN_API(nvnCommandBufferClearDepthStencil) {
    *REC(cmd_of(c), OP_CLEAR_DS, p_clear_ds) = (p_clear_ds){arg_s(c, 0), nvn_arg_bool(c, 1), (int)arg_x(c, 2), (int)arg_x(c, 3)};
    cmd_rt_trace(c, "ClearDepthStencil");
}
static void set_viewports(cmd_t *o, int first, int n, const float *v) {
    if (first < 0 || n < 0 || first + n > 16) {
        rt_log("nvn: SetViewports first %d n %d", first, n);
        return;
    }
    // Only n entries are read by replay; keeping all 16 in every one-viewport
    // recording amplified retained control-ring copies over long sessions.
    p_viewports *r = RECN(o, OP_VIEWPORTS, p_viewports, v, n);
    r->first = first;
    r->n = n;
    for (int i = 0; i < n; i++) memcpy(r->v[i], v + 4 * i, 16);
    hw_fix(o, (8 + sizeof(p_viewports) + 7) & ~(size_t)7); // preserve existing guest accounting
}
// void nvnCommandBufferSetViewports(NVNcommandBuffer *cmd, int first, int n, const float *xywh)
NVN_API(nvnCommandBufferSetViewports) { set_viewports(cmd_of(c), (int)arg_x(c, 1), (int)arg_x(c, 2), arg_p(c, 3)); }
// void nvnCommandBufferSetViewport(NVNcommandBuffer *cmd, int x, int y, int w, int h)
NVN_API(nvnCommandBufferSetViewport) {
    float v[4] = {(float)(int)arg_x(c, 1), (float)(int)arg_x(c, 2), (float)(int)arg_x(c, 3), (float)(int)arg_x(c, 4)};
    set_viewports(cmd_of(c), 0, 1, v);
}
static void set_scissor(cmd_t *o, int x, int y, int w, int h) { *REC(o, OP_SCISSOR, p_scissor) = (p_scissor){x, y, w, h}; }
// void nvnCommandBufferSetScissor(NVNcommandBuffer *cmd, int x, int y, int w, int h)
NVN_API(nvnCommandBufferSetScissor) {
    set_scissor(cmd_of(c), (int)arg_x(c, 1), (int)arg_x(c, 2), (int)arg_x(c, 3), (int)arg_x(c, 4));
}
// void nvnCommandBufferSetScissors(NVNcommandBuffer *cmd, int first, int n, const int *xywh): the replay keeps one
// scissor (index 0); other indices matter only to shaders that select a viewport index, so differing ones are reported
NVN_API(nvnCommandBufferSetScissors) {
    int first = (int)arg_x(c, 1), n = (int)arg_x(c, 2);
    const int *r = arg_p(c, 3);
    if (first < 0 || n < 0 || first + n > 16) rt_fatal("nvn: SetScissors first %d n %d", first, n);
    for (int i = 1; i < n; i++)
        if (memcmp(r + 4 * i, r, 16)) {
            static int once;
            if (!once++) rt_log("nvn: SetScissors: differing scissor %d ignored (single-scissor replay)", first + i);
        }
    if (first == 0 && n > 0) set_scissor(cmd_of(c), r[0], r[1], r[2], r[3]);
}
static void set_depth_range(cmd_t *o, float n, float f) { *REC(o, OP_DEPTH_RANGE, p_depth_range) = (p_depth_range){n, f}; }
// void nvnCommandBufferSetDepthRange(NVNcommandBuffer *cmd, float near, float far)
NVN_API(nvnCommandBufferSetDepthRange) { set_depth_range(cmd_of(c), arg_s(c, 0), arg_s(c, 1)); }
// void nvnCommandBufferSetDepthRanges(NVNcommandBuffer *cmd, int first, int n, const float *ranges)
NVN_API(nvnCommandBufferSetDepthRanges) {
    int first = (int)arg_x(c, 1), n = (int)arg_x(c, 2);
    const float *r = arg_p(c, 3);
    if (first < 0 || n < 0 || first + n > 16) rt_fatal("nvn: SetDepthRanges first %d n %d", first, n);
    for (int i = 1; i < n; i++)
        if (memcmp(r + 2 * i, r, 8)) {
            static int once;
            if (!once++) rt_log("nvn: SetDepthRanges: differing range %d ignored (single-range replay)", first + i);
        }
    if (first == 0 && n > 0) set_depth_range(cmd_of(c), r[0], r[1]);
}
// void nvnCommandBufferSetDepthClamp / SetRenderEnable / SetRasterizerDiscard(NVNcommandBuffer *cmd, NVNboolean enable)
NVN_API(nvnCommandBufferSetDepthClamp) { REC(cmd_of(c), OP_DEPTH_CLAMP, p_flag)->value = nvn_arg_bool(c, 1) != 0; }
NVN_API(nvnCommandBufferSetRenderEnable) {
    REC(cmd_of(c), OP_RENDER_ENABLE, p_flag)->value = nvn_arg_bool(c, 1) != 0;
    cmd_rt_trace(c, "SetRenderEnable");
}
// Rasterizer discard runs vertex work without rasterising; with no transform feedback that equals not drawing.
NVN_API(nvnCommandBufferSetRasterizerDiscard) { REC(cmd_of(c), OP_RASTER_DISCARD, p_flag)->value = nvn_arg_bool(c, 1) != 0; }
// void nvnCommandBufferSetPrimitiveRestart(NVNcommandBuffer *cmd, NVNboolean enable, int index): TOTK draws its
// terrain and cliff strips with 16-bit indices and restart at 0xffff; the replay applies it per indexed draw (op_draw_idx)
NVN_API(nvnCommandBufferSetPrimitiveRestart) {
    *REC(cmd_of(c), OP_PRIM_RESTART, p_prim_restart) = (p_prim_restart){nvn_arg_bool(c, 1) != 0, (uint32_t)arg_x(c, 2)};
}
// void nvnCommandBufferSetRenderEnableConditional(NVNcommandBuffer *cmd, NVNconditionalRenderMode mode,
// NVNbufferAddress counters): rendering is enabled when the two NVNcounterData.counter values at addr / addr+16
// compare equal (mode 0) or not equal (mode 1). TOTK gates most of its ClearColor commands with it; the replay
// evaluates it in GPU order.
NVN_API(nvnCommandBufferSetRenderEnableConditional) {
    int m = (int)arg_x(c, 1);
    if (m != 0 && m != 1) rt_fatal("nvn: SetRenderEnableConditional: unknown mode %d", m);
    p_render_cond *r = REC(cmd_of(c), OP_RENDER_COND, p_render_cond);
    r->mode = m;
    r->addr = arg_x(c, 2);
    cmd_rt_trace(c, "SetRenderEnableConditional");
}
// void nvnCommandBufferBindProgram(NVNcommandBuffer *cmd, const NVNprogram *program, int stages)
NVN_API(nvnCommandBufferBindProgram) {
    *REC(cmd_of(c), OP_BIND_PROGRAM, p_bind_program) = (p_bind_program){arg_p(c, 1), (int)arg_x(c, 2)};
}
// void nvnCommandBufferBind{Blend,Color,ChannelMask,Multisample,Polygon,DepthStencil}State(NVNcommandBuffer *cmd,
// const NVN*State *state): the state object's bytes are copied (p_bind_state.kind order)
static void bind_state(const Cpu *c, int kind, size_t n) {
    cmd_t *o = cmd_of(c);
    p_bind_state *r = rec(o, OP_BIND_STATE, offsetof(p_bind_state, bytes) + n);
    r->kind = kind;
    memcpy(r->bytes, arg_p(c, 1), n);
    hw_fix(o, (8 + sizeof(p_bind_state) + 7) & ~(size_t)7);
}
NVN_API(nvnCommandBufferBindBlendState) { bind_state(c, 0, sizeof(blend_st)); }
NVN_API(nvnCommandBufferBindColorState) { bind_state(c, 1, sizeof(color_st)); }
NVN_API(nvnCommandBufferBindChannelMaskState) { bind_state(c, 2, sizeof(chmask_st)); }
NVN_API(nvnCommandBufferBindMultisampleState) { bind_state(c, 3, sizeof(ms_st)); }
NVN_API(nvnCommandBufferBindPolygonState) { bind_state(c, 4, sizeof(poly_st)); }
NVN_API(nvnCommandBufferBindDepthStencilState) { bind_state(c, 5, sizeof(ds_st)); }
// void nvnCommandBufferSetPointSize(NVNcommandBuffer *cmd, float size): recorded as bind-state kind 6 with the float as payload
NVN_API(nvnCommandBufferSetPointSize) {
    cmd_t *o = cmd_of(c);
    p_bind_state *r = rec(o, OP_BIND_STATE, offsetof(p_bind_state, bytes) + sizeof(float));
    float s = arg_s(c, 0);
    r->kind = 6;
    memcpy(r->bytes, &s, sizeof s);
    hw_fix(o, (8 + sizeof(p_bind_state) + 7) & ~(size_t)7);
}
// void nvnCommandBufferBindVertexAttribState(NVNcommandBuffer *cmd, int n, const NVNvertexAttribState *states)
NVN_API(nvnCommandBufferBindVertexAttribState) {
    cmd_t *o = cmd_of(c);
    int n = (int)arg_x(c, 1);
    if (n < 0 || n > 16) { rt_log("nvn: BindVertexAttribState n %d", n); return; }
    rec_array(o, OP_BIND_VATTR, 0, n, arg_p(c, 2), 4);
    hw_fix(o, 68);   // the driver always writes all 16 attribute formats: 17 words
}
// void nvnCommandBufferBindVertexStreamState(NVNcommandBuffer *cmd, int n, const NVNvertexStreamState *states)
NVN_API(nvnCommandBufferBindVertexStreamState) {
    cmd_t *o = cmd_of(c);
    int n = (int)arg_x(c, 1);
    if (n < 0 || n > 16) { rt_log("nvn: BindVertexStreamState n %d", n); return; }
    rec_array(o, OP_BIND_VSTREAM, 0, n, arg_p(c, 2), 8);
    hw_fix(o, 4 * (16 + (size_t)n));
}
static void bind_vertex_buffers(cmd_t *o, int first, int n, const NVNbufferRange *rg) {
    if (first < 0 || n < 0 || first + n > 16) { rt_log("nvn: BindVertexBuffers first %d n %d", first, n); return; }
    rec_array(o, OP_BIND_VBUF, first, n, rg, sizeof *rg);
    hw_fix(o, 24 * (size_t)n);
}
// void nvnCommandBufferBindVertexBuffers(NVNcommandBuffer *cmd, int first, int n, const NVNbufferRange *ranges)
NVN_API(nvnCommandBufferBindVertexBuffers) { bind_vertex_buffers(cmd_of(c), (int)arg_x(c, 1), (int)arg_x(c, 2), arg_p(c, 3)); }
// void nvnCommandBufferBindVertexBuffer(NVNcommandBuffer *cmd, int index, NVNbufferAddress address, size_t size)
NVN_API(nvnCommandBufferBindVertexBuffer) {
    NVNbufferRange r = {arg_x(c, 2), arg_x(c, 3)};
    bind_vertex_buffers(cmd_of(c), (int)arg_x(c, 1), 1, &r);
}
// Uniform and storage buffers: one record per binding (CB_SIZE/ADDRESS 4 words + CB_BIND 1 on hardware).
static void rec_buffer(cmd_t *o, uint32_t op, p_bind_buf value) {
    size_t size = nvn_buffer_pack(NULL, &value);
    nvn_buffer_pack(rec(o, op, size), &value);
    hw_fix(o, 20);
}
static void bind_buffers(const Cpu *c, uint32_t op) {   // (cmd, stage, first, n, const NVNbufferRange *ranges)
    cmd_t *o = cmd_of(c);
    int st = (int)arg_x(c, 1), first = (int)arg_x(c, 2), n = (int)arg_x(c, 3);
    const NVNbufferRange *rg = arg_p(c, 4);
    for (int i = 0; i < n; i++) {
        rec_buffer(o, op, (p_bind_buf){st, first + i, rg[i].address, rg[i].size});
    }
}
static void bind_buffer(const Cpu *c, uint32_t op) {    // (cmd, stage, index, NVNbufferAddress address, size_t size)
    cmd_t *o = cmd_of(c);
    rec_buffer(o, op, (p_bind_buf){(int)arg_x(c, 1), (int)arg_x(c, 2), arg_x(c, 3), arg_x(c, 4)});
}
NVN_API(nvnCommandBufferBindUniformBuffer) { bind_buffer(c, OP_BIND_UBO); }
NVN_API(nvnCommandBufferBindUniformBuffers) { bind_buffers(c, OP_BIND_UBO); }
NVN_API(nvnCommandBufferBindStorageBuffer) { bind_buffer(c, OP_BIND_SSBO); }
NVN_API(nvnCommandBufferBindStorageBuffers) { bind_buffers(c, OP_BIND_SSBO); }
// void nvnCommandBufferPush{Graphics,Compute}Constants(NVNcommandBuffer *, int table, int offset, int bytes,
// const void *data), NVN 2. The driver (sdk+0x5d34b0 graphics, sdk+0x5d3a30 compute) clamps bytes to 0x100, drops
// calls under 4 bytes and writes words: graphics as SET_ROOT_TABLE_SELECTOR (method 0x504, table & 7 | offset << 8)
// plus LOAD_ROOT_TABLE (0x508), compute through MME macro 0x53. Graphics shaders read table 0 as constant bank 24:
// the full-screen blits get their texel size and size this way (1/w, 1/h, w, h at 0x0..0xc in Bananza's runs). Where
// compute shaders read their pushes is not known yet; no Bananza compute stage reads bank 24.
// The Deferred entry points (sdk+0x5d3670 graphics, sdk+0x5d3df0 compute) have the same five arguments, but store the
// direct data pointer and the low byte of offset without reading data. The submit interpreter reloads that pointer.
// Keep the host's 256-byte table bound for both pipelines; the deferred compute helper itself does not clamp bytes.
static void push_constants(const Cpu *c, int compute, int deferred) {
    cmd_t *o = cmd_of(c);
    int table = (int)(arg_x(c, 1) & 7), offset = deferred ? (uint8_t)arg_x(c, 2) : (int)arg_x(c, 2);
    uint32_t bytes = (uint32_t)arg_x(c, 3);
    if (bytes > 0x100) bytes = 0x100;
    if (bytes < 4) return;
    if (offset < 0 || offset + bytes > 0x100 || (offset & 3)) {
        rt_log("nvn: Push%sConstants offset %#x bytes %#x past the 256-byte root table", compute ? "Compute" : "Graphics",
               offset, bytes);
        return;
    }
    uint32_t n = bytes / 4;
    p_push_const *r = RECN(o, OP_PUSH_CONST, p_push_const, w, n);   // holds only n words of w: set fields one by one
    r->compute = compute, r->table = table, r->offset = offset, r->bytes = (int32_t)(n * 4);
    r->src = deferred ? arg_x(c, 4) : 0;
    if (!deferred) memcpy(r->w, arg_p(c, 4), n * 4);
    hw_fix(o, 4 * ((size_t)n + 2));   // method header, selector or macro parameter, n words
}
NVN_API(nvnCommandBufferPushGraphicsConstants) { push_constants(c, 0, 0); }
NVN_API(nvnCommandBufferPushComputeConstants) { push_constants(c, 1, 0); }
NVN_API(nvnCommandBufferPushGraphicsConstantsDeferred) { push_constants(c, 0, 1); }
NVN_API(nvnCommandBufferPushComputeConstantsDeferred) { push_constants(c, 1, 1); }
// Texture, image and separate handle binds. A deferred bind (NVN 55 *Deferred entry points, the fallback of the inline
// fast paths) keeps the pointer to the game's handle array: the original driver (sdk+0x46cc00 BindTexturesDeferred)
// reads the handles when the command buffer is submitted, and the game fills its material handle tables after
// recording and before the submit (nvn_submit_prepare). hw_bytes: the hardware size of the plain variant.
static void bind_handles(cmd_t *o, uint32_t op, int st, int first, int n, const uint64_t *h, int deferred, size_t hw_bytes) {
    if (n < 0 || n > 32) { rt_log("nvn: bind of %d handles", n); return; }
    size_t compact = n == 1 && !deferred ? nvn_handle_pack_one(NULL, st, first, 0) : 0;
    if (compact) {
        uint64_t value; memcpy(&value, h, sizeof value);
        nvn_handle_pack_one(rec(o, op, compact), st, first, value);
    } else {
        p_bind_handles *r = RECN(o, op, p_bind_handles, h, n);
        r->stage = st;
        r->first = first;
        r->n = n;
        r->src = deferred ? (uint64_t)(uintptr_t)h : 0;
        memcpy(r->h, h, 8 * (size_t)n);
    }
    hw_fix(o, hw_bytes ? hw_bytes : 16 + 4 * (size_t)n);   // macro 7 + method 0x1a2c + CB_POS + one word per binding
}
static void bind_handle_array(const Cpu *c, uint32_t op, int deferred, int wide) {   // (cmd, stage, first, n, handles)
    int n = (int)arg_x(c, 3);
    bind_handles(cmd_of(c), op, (int)arg_x(c, 1), (int)arg_x(c, 2), n, arg_p(c, 4), deferred,
                 wide && n >= 0 && n <= 32 ? 16 + 8 * (size_t)n : 0);
}
static void bind_one_handle(const Cpu *c, uint32_t op, size_t hw_bytes) {   // (cmd, stage, index, handle)
    uint64_t v = arg_x(c, 3);
    bind_handles(cmd_of(c), op, (int)arg_x(c, 1), (int)arg_x(c, 2), 1, &v, 0, hw_bytes);
}
// Combined texture handles share the texture slots with separate textures (sampler id in bits 20 and up). Images
// (writable textures): handle = texture pool index; the game does not call RegisterImage, images and textures share
// their registration.
NVN_API(nvnCommandBufferBindTexture) { bind_one_handle(c, OP_BIND_TEX, 0); }
NVN_API(nvnCommandBufferBindTextures) { bind_handle_array(c, OP_BIND_TEX, 0, 0); }
NVN_API(nvnCommandBufferBindImage) { bind_one_handle(c, OP_BIND_IMG, 24); }
NVN_API(nvnCommandBufferBindImages) { bind_handle_array(c, OP_BIND_IMG, 0, 1); }
NVN_API(nvnCommandBufferBindSeparateTexture) { bind_one_handle(c, OP_BIND_TEX, 24); }
NVN_API(nvnCommandBufferBindSeparateTextures) { bind_handle_array(c, OP_BIND_TEX, 0, 1); }
NVN_API(nvnCommandBufferBindSeparateSampler) { bind_one_handle(c, OP_BIND_SAMP, 24); }
NVN_API(nvnCommandBufferBindSeparateSamplers) { bind_handle_array(c, OP_BIND_SAMP, 0, 1); }
// void nvnCommandBufferBind{Textures,Images,SeparateTextures,SeparateSamplers}Deferred(NVNcommandBuffer *cmd,
// NVNshaderStage stage, int first, int n, const T *handles) (NVN 55, no prototype in nvn.h)
NVN_API(nvnCommandBufferBindTexturesDeferred) { bind_handle_array(c, OP_BIND_TEX, 1, 0); }
NVN_API(nvnCommandBufferBindImagesDeferred) { bind_handle_array(c, OP_BIND_IMG, 1, 1); }
NVN_API(nvnCommandBufferBindSeparateTexturesDeferred) { bind_handle_array(c, OP_BIND_TEX, 1, 1); }
NVN_API(nvnCommandBufferBindSeparateSamplersDeferred) { bind_handle_array(c, OP_BIND_SAMP, 1, 1); }
// void nvnCommandBufferBind{Uniform,Storage}BuffersDeferred(NVNcommandBuffer *cmd, NVNshaderStage stage, int first,
// int n, const NVNbufferRange *ranges): buffer ranges still bind at record time
NVN_API(nvnCommandBufferBindUniformBuffersDeferred) {
    static int once;
    if (!once++) NVN_LOG("BindUniformBuffersDeferred: buffer ranges bind at record time");
    bind_buffers(c, OP_BIND_UBO);
}
NVN_API(nvnCommandBufferBindStorageBuffersDeferred) {
    static int once;
    if (!once++) NVN_LOG("BindStorageBuffersDeferred: buffer ranges bind at record time");
    bind_buffers(c, OP_BIND_SSBO);
}
// void nvnCommandBufferSetStencil{ValueMask,Mask,Ref}(NVNcommandBuffer *cmd, NVNface face, int value)
NVN_API(nvnCommandBufferSetStencilValueMask) {
    *REC(cmd_of(c), OP_STENCIL, p_stencil) = (p_stencil){(int)arg_x(c, 1), (int)arg_x(c, 2), 0, 0, 0};
}
NVN_API(nvnCommandBufferSetStencilMask) {
    *REC(cmd_of(c), OP_STENCIL, p_stencil) = (p_stencil){(int)arg_x(c, 1), 0, (int)arg_x(c, 2), 0, 1};
}
NVN_API(nvnCommandBufferSetStencilRef) {
    *REC(cmd_of(c), OP_STENCIL, p_stencil) = (p_stencil){(int)arg_x(c, 1), 0, 0, (int)arg_x(c, 2), 2};
}
// void nvnCommandBufferSetStencilCullCriteria(NVNcommandBuffer *, NVNstencilFunc, int ref, int mask): a hint for the
// hardware's stencil culling; the stencil test itself still decides, so the Vulkan replay needs nothing (Bananza)
NVN_API(nvnCommandBufferSetStencilCullCriteria) { nvn_copy_mode_unsupported(c, "SetStencilCullCriteria"); }
// void nvnCommandBufferSetPolygonOffsetClamp(NVNcommandBuffer *cmd, float factor, float units, float clamp)
NVN_API(nvnCommandBufferSetPolygonOffsetClamp) {
    *REC(cmd_of(c), OP_POLY_OFFSET, p_poly_offset) = (p_poly_offset){arg_s(c, 0), arg_s(c, 1), arg_s(c, 2)};
}
// void nvnCommandBufferDrawArrays(NVNcommandBuffer *cmd, NVNdrawPrimitive prim, int first, int count)
NVN_API(nvnCommandBufferDrawArrays) {
    cmd_rt_trace(c, "DrawArrays");
    *REC(cmd_of(c), OP_DRAW, p_draw) = (p_draw){(int)arg_x(c, 1), (int)arg_x(c, 2), (int)arg_x(c, 3), 0, 1};
}
// void nvnCommandBufferDrawArraysInstanced(NVNcommandBuffer *cmd, NVNdrawPrimitive prim, int first, int count,
// int base_instance, int instances): zero instances draw nothing
NVN_API(nvnCommandBufferDrawArraysInstanced) {
    cmd_rt_trace(c, "DrawArraysInstanced");
    int inst = (int)arg_x(c, 5);
    if (inst <= 0) return;
    *REC(cmd_of(c), OP_DRAW, p_draw) = (p_draw){(int)arg_x(c, 1), (int)arg_x(c, 2), (int)arg_x(c, 3), (int)arg_x(c, 4), inst};
}
// void nvnCommandBufferDrawElements(NVNcommandBuffer *cmd, NVNdrawPrimitive prim, NVNindexType type, int count,
// NVNbufferAddress indices)
static void rec_indexed(cmd_t *o, p_draw_idx value) {
    size_t size = nvn_indexed_pack(NULL, &value);
    nvn_indexed_pack(rec(o, OP_DRAW_IDX, size), &value);
}
NVN_API(nvnCommandBufferDrawElements) {
    cmd_rt_trace(c, "DrawElements");
    cmd_t *o = cmd_of(c);
    rec_indexed(o, (p_draw_idx){(int)arg_x(c, 1), (int)arg_x(c, 2), (int)arg_x(c, 3), 0, 0, 1, arg_x(c, 4)});
    hw_fix(o, 48);
}
// void nvnCommandBufferDrawElementsBaseVertex(..., NVNbufferAddress indices, int base_vertex)
NVN_API(nvnCommandBufferDrawElementsBaseVertex) {
    cmd_rt_trace(c, "DrawElementsBaseVertex");
    cmd_t *o = cmd_of(c);
    rec_indexed(o, (p_draw_idx){(int)arg_x(c, 1), (int)arg_x(c, 2), (int)arg_x(c, 3), (int)arg_x(c, 5), 0, 1, arg_x(c, 4)});
    hw_fix(o, 44);
}
// void nvnCommandBufferDrawElementsInstanced(..., NVNbufferAddress indices, int base_vertex, int base_instance,
// int instances): assumed to use the 6-parameter draw macro of the base-vertex form (unverified)
NVN_API(nvnCommandBufferDrawElementsInstanced) {
    cmd_rt_trace(c, "DrawElementsInstanced");
    cmd_t *o = cmd_of(c);
    rec_indexed(o, (p_draw_idx){(int)arg_x(c, 1), (int)arg_x(c, 2), (int)arg_x(c, 3),
                              (int)arg_x(c, 5), (int)arg_x(c, 6), (int)arg_x(c, 7), arg_x(c, 4)});
    hw_fix(o, 44);
}
// void nvnCommandBufferDrawArraysIndirect(NVNcommandBuffer *cmd, NVNdrawPrimitive prim, NVNbufferAddress indirect)
// Same three-argument entry as nxnative; x3 is not a draw count.
NVN_API(nvnCommandBufferDrawArraysIndirect) {
    cmd_rt_trace(c, "DrawArraysIndirect");
    *REC(cmd_of(c), OP_DRAW_IND, p_draw_ind) = (p_draw_ind){(int)arg_x(c, 1), 0, arg_x(c, 2)};
}
// void nvnCommandBufferDrawElementsIndirect(NVNcommandBuffer *cmd, NVNdrawPrimitive prim, NVNindexType type,
// NVNbufferAddress indices, NVNbufferAddress indirect): the arguments at indirect are often GPU-written, so the replay
// draws with vkCmdDrawIndexedIndirect from the pool buffer (nxnative hle_nvn.c, SavageWindRecomp cmdbuf.c)
NVN_API(nvnCommandBufferDrawElementsIndirect) {
    cmd_rt_trace(c, "DrawElementsIndirect");
    *REC(cmd_of(c), OP_DRAW_IDX_IND, p_draw_idx_ind) = (p_draw_idx_ind){(int)arg_x(c, 1), (int)arg_x(c, 2), arg_x(c, 3), arg_x(c, 4)};
}
// void nvnCommandBufferDispatchCompute(NVNcommandBuffer *cmd, int x, int y, int z): caller = the game's return address
NVN_API(nvnCommandBufferDispatchCompute) {
    *REC(cmd_of(c), OP_DISPATCH, p_dispatch) = (p_dispatch){(int)arg_x(c, 1), (int)arg_x(c, 2), (int)arg_x(c, 3), c->x[30]};
}
// void nvnCommandBufferDispatchComputeIndirect(NVNcommandBuffer *cmd, NVNbufferAddress indirect): vkCmdDispatchIndirect
NVN_API(nvnCommandBufferDispatchComputeIndirect) {
    *REC(cmd_of(c), OP_DISPATCH_IND, p_dispatch_ind) = (p_dispatch_ind){arg_x(c, 1), c->x[30]};
}
// void nvnCommandBufferClearBuffer(NVNcommandBuffer *cmd, NVNbufferAddress addr, size_t size, uint32_t value)
NVN_API(nvnCommandBufferClearBuffer) {
    *REC(cmd_of(c), OP_CLEAR_BUF, p_clear_buf) = (p_clear_buf){arg_x(c, 1), arg_x(c, 2), (uint32_t)arg_x(c, 3), 0};
}
// void nvnCommandBufferReportValue(NVNcommandBuffer *cmd, uint32_t value, NVNbufferAddress addr): the value lands when
// the GPU reaches this point. Recorded as a 4-byte ClearBuffer, so it stays in GPU order with the ClearBuffer resets of
// the same counter (main+0x7b9274 modes 1-3) and works in CPU-invisible pools; the CPU sees it after the fence, as a
// counter report.
NVN_API(nvnCommandBufferReportValue) {
    *REC(cmd_of(c), OP_CLEAR_BUF, p_clear_buf) = (p_clear_buf){arg_x(c, 2), 4, (uint32_t)arg_x(c, 1), 0};
}
// Colour reduction is a Maxwell bandwidth hint for blending into compressed targets: no effect on the result.
// void nvnCommandBufferSetColorReductionEnable(cmd, NVNboolean enable), SetColorReductionThresholds(cmd, class, const int *)
NVN_API(nvnCommandBufferSetColorReductionEnable) { nvn_copy_mode_unsupported(c, "SetColorReductionEnable"); }
NVN_API(nvnCommandBufferSetColorReductionThresholds) { nvn_copy_mode_unsupported(c, "SetColorReductionThresholds"); }
// void nvnCommandBufferBarrier(NVNcommandBuffer *cmd, int bits)
NVN_API(nvnCommandBufferBarrier) { REC(cmd_of(c), OP_BARRIER, p_barrier)->bits = (int)arg_x(c, 1); }
// void nvnCommandBufferSetTexturePool / SetSamplerPool(NVNcommandBuffer *cmd, const NVNtexturePool / NVNsamplerPool *)
NVN_API(nvnCommandBufferSetTexturePool) {
    dpool_ref pool;
    if (!nvn_dpool_snapshot(arg_p(c, 1), 1u << 20, &pool)) rt_fatal("nvn: invalid texture descriptor pool");
    REC(cmd_of(c), OP_SET_TEX_POOL, p_set_pool)->pool = pool;
}
NVN_API(nvnCommandBufferSetSamplerPool) {
    dpool_ref pool;
    if (!nvn_dpool_snapshot(arg_p(c, 1), 1u << 12, &pool)) rt_fatal("nvn: invalid sampler descriptor pool");
    REC(cmd_of(c), OP_SET_SAMP_POOL, p_set_pool)->pool = pool;
}
// void nvnCommandBufferCopyBufferToTexture(NVNcommandBuffer *cmd, NVNbufferAddress src, const NVNtexture *dst,
// const NVNtextureView *view, const NVNcopyRegion *region, int flags)
NVN_API(nvnCommandBufferCopyBufferToTexture) {
    p_copy_b2t *o = REC(cmd_of(c), OP_COPY_B2T, p_copy_b2t);
    o->src = arg_x(c, 1);
    o->tex = tex_pin(arg_p(c, 2));
    copy_view(&o->view, &o->has_view, arg_p(c, 3));
    o->r = *(const NVNcopyRegion *)arg_p(c, 4);
    o->flags = (int)arg_x(c, 5);
}
// void nvnCommandBufferCopyTextureToBuffer(NVNcommandBuffer *cmd, const NVNtexture *src, const NVNtextureView *view,
// const NVNcopyRegion *region, NVNbufferAddress dst, int flags): p_copy_b2t with src = the destination address
NVN_API(nvnCommandBufferCopyTextureToBuffer) {
    p_copy_b2t *o = REC(cmd_of(c), OP_COPY_T2B, p_copy_b2t);
    o->src = arg_x(c, 4);
    o->tex = tex_pin(arg_p(c, 1));
    copy_view(&o->view, &o->has_view, arg_p(c, 2));
    o->r = *(const NVNcopyRegion *)arg_p(c, 3);
    o->flags = (int)arg_x(c, 5);
}
// void nvnCommandBufferClearTexture{,ui}(NVNcommandBuffer *cmd, const NVNtexture *tex, const NVNtextureView *view,
// const NVNcopyRegion *region, const float / uint32_t *rgba, int mask)
static void clear_texture(const Cpu *c, int is_uint) {
    p_clear_tex *o = REC(cmd_of(c), OP_CLEAR_TEX, p_clear_tex);
    o->tex = tex_pin(arg_p(c, 1));
    copy_view(&o->view, &o->has_view, arg_p(c, 2));
    o->r = *(const NVNcopyRegion *)arg_p(c, 3);
    o->mask = (int)arg_x(c, 5);
    o->is_uint = (uint8_t)is_uint;
    memcpy(o->c.u, arg_p(c, 4), 16);
}
NVN_API(nvnCommandBufferClearTexture) { clear_texture(c, 0); }
NVN_API(nvnCommandBufferClearTextureui) { clear_texture(c, 1); }
// void nvnCommandBufferCopyTextureToTexture(NVNcommandBuffer *cmd, const NVNtexture *src, const NVNtextureView *src_view,
// const NVNcopyRegion *src_region, const NVNtexture *dst, const NVNtextureView *dst_view,
// const NVNcopyRegion *dst_region, int flags)
NVN_API(nvnCommandBufferCopyTextureToTexture) {
    p_copy_t2t *o = REC(cmd_of(c), OP_COPY_T2T, p_copy_t2t);
    o->src = tex_pin(arg_p(c, 1));
    o->dst = tex_pin(arg_p(c, 4));
    copy_view(&o->sv, &o->has_sv, arg_p(c, 2));
    copy_view(&o->dv, &o->has_dv, arg_p(c, 5));
    o->sr = *(const NVNcopyRegion *)arg_p(c, 3);
    o->dr = *(const NVNcopyRegion *)arg_p(c, 6);
    o->flags = (int)arg_x(c, 7);
}
// void nvnCommandBufferDownsample(NVNcommandBuffer *cmd, const NVNtexture *src, const NVNtexture *dst): level 0 of src
// into level 0 of dst, whole extents, filtered. Recorded as a linear-filter texture copy: copy_t2t resolves a
// multisample source into an equal-size target and blits (2x2 average at half size) between sizes. As nxnative
// hle_nvn.c and SavageWindRecomp cmdbuf.c record it; docs/maxwell.md 1.19.
NVN_API(nvnCommandBufferDownsample) {
    nvn_copy_mode_unsupported(c, "Downsample"); // filtered/resolve semantics were not established for mode 2
    p_copy_t2t *o = REC(cmd_of(c), OP_COPY_T2T, p_copy_t2t);
    o->src = tex_pin(arg_p(c, 1));
    o->dst = tex_pin(arg_p(c, 2));
    if (o->src) o->sr = (NVNcopyRegion){0, 0, 0, (int)o->src->d.width, (int)o->src->d.height, 1};
    if (o->dst) o->dr = (NVNcopyRegion){0, 0, 0, (int)o->dst->d.width, (int)o->dst->d.height, 1};
    o->flags = NVN_COPY_FLAGS_LINEAR_FILTER_BIT;
}
// void nvnCommandBufferSetCopyRowStride(NVNcommandBuffer *cmd, ptrdiff_t stride)
NVN_API(nvnCommandBufferSetCopyRowStride) { REC(cmd_of(c), OP_COPY_STRIDE, p_copy_stride)->stride = (int64_t)arg_x(c, 1); }
// void nvnCommandBufferSetCopyImageStride(NVNcommandBuffer *cmd, ptrdiff_t stride)
NVN_API(nvnCommandBufferSetCopyImageStride) {
    int64_t stride = (int64_t)arg_x(c, 1);
    if (stride < 0) rt_fatal("nvn: negative copy image stride %lld is unsupported", (long long)stride);
    REC(cmd_of(c), OP_COPY_IMAGE_STRIDE, p_copy_stride)->stride = stride;
}
// void nvnCommandBufferSetBlendColor(NVNcommandBuffer *cmd, const float *rgba)
NVN_API(nvnCommandBufferSetBlendColor) { memcpy(REC(cmd_of(c), OP_BLEND_COLOR, p_blend_color)->rgba, arg_p(c, 1), 16); }

// void nvnCommandBufferWaitSync(NVNcommandBuffer *cmd, const NVNsync *sync): the SDK retains the pointer at record
// time and reads the sync's current producer at submit. Preparation fulfils the wait before consumer CPU snapshots.
NVN_API(nvnCommandBufferWaitSync) {
    cmd_t *o = cmd_of(c);
    REC(o, OP_WAIT_SYNC, p_wait_sync)->sync = arg_p(c, 1);
    hw_fix(o, 0);   // the SDK records a CPU control entry here; GPU wait commands are emitted at submit
}
// void nvnCommandBufferFenceSync(cmd, sync, condition, flags): retain the output
// pointer without reading or changing it while recording. The current backend
// publishes a whole-batch completion frontier after submission; condition 1 is
// therefore conservatively later than just graphics-world-space completion.
static void fence_parameters(uint32_t condition, uint32_t flags) {
    if ((condition != NVN_SYNC_CONDITION_ALL_GPU_COMMANDS_COMPLETE &&
         condition != NVN_SYNC_CONDITION_GRAPHICS_WORLD_SPACE_COMPLETE) || flags != 0)
        rt_fatal("nvn: CommandBufferFenceSync unsupported condition %u flags %#x", condition, flags);
}
NVN_API(nvnCommandBufferFenceSync) {
    cmd_t *o = cmd_of(c);
    sync_t *sync = arg_p(c, 1);
    uint32_t condition = (uint32_t)arg_x(c, 2), flags = (uint32_t)arg_x(c, 3);
    if (!o || o->magic != CMD_MAGIC || !o->recording || !sync)
        rt_fatal("nvn: CommandBufferFenceSync requires a recording buffer and nonnull sync");
    fence_parameters(condition, flags);
    *REC(o, OP_FENCE_SYNC, p_fence_sync) = (p_fence_sync){sync, condition, flags};
    hw_fix(o, 0); // host control entry; no invented hardware push-buffer encoding
}
// Commands without an effect on this replay (hle_nvn.c g_nops). WaitEvent still relies on in-order replay; shader
// scratch memory and the tessellation patch size have no host counterpart the replay needs.
// void nvnCommandBufferWaitEvent(cmd, const NVNevent *event,
// NVNeventWaitMode mode, uint32_t value), SetShaderScratchMemory(cmd, const NVNmemoryPool *pool, ptrdiff_t offset,
// size_t size), SetPatchSize(cmd, int size)
NVN_API(nvnCommandBufferWaitEvent) { nvn_copy_mode_unsupported(c, "WaitEvent"); }
NVN_API(nvnCommandBufferSetShaderScratchMemory) { nvn_copy_mode_unsupported(c, "SetShaderScratchMemory"); }
NVN_API(nvnCommandBufferSetPatchSize) { nvn_copy_mode_unsupported(c, "SetPatchSize"); }

// ---------- submit
// What the console reads at the submit call is read here, on the game thread: every handle must be a live recording,
// and deferred binds and pushes take the data the game's arrays hold now (the game fills its tables for the next frame
// as soon as the submit returns). The result is written into the recording copy.
static uint64_t def_reads, def_changed, n_clones;

struct NvnFenceBatch { sync_t **syncs; size_t count, capacity; };

static void fence_list_add(NvnFenceBatch *batch, sync_t *sync) {
    if (batch->count == batch->capacity) {
        size_t capacity = batch->capacity ? batch->capacity * 2 : 16;
        if (capacity < batch->capacity || capacity > SIZE_MAX / sizeof *batch->syncs)
            rt_fatal("nvn: recorded fence list too large");
        sync_t **memory = realloc(batch->syncs, capacity * sizeof *memory);
        if (!memory) rt_fatal("nvn: recorded fence list allocation failed");
        batch->syncs = memory;
        batch->capacity = capacity;
    }
    batch->syncs[batch->count++] = sync;
}

static int fence_list_has(const NvnFenceBatch *batch, const sync_t *sync) {
    for (size_t i = 0; i < batch->count; i++) if (batch->syncs[i] == sync) return 1;
    return 0;
}

NvnFenceBatch *nvn_submit_capture_fences(const uint64_t *handles, int n) {
    NvnFenceBatch *batch = NULL;
    for (int i = 0; i < n; i++) {
        uint32_t sync_ops;
        uint64_t bytes = copy_info(handles[i], &sync_ops);
        if (!bytes) rt_fatal("nvn: recorded-fence capture needs a live command handle");
        if ((sync_ops & COPY_SYNC_KNOWN) && !(sync_ops & COPY_SYNC_FENCE)) continue;
        const uint8_t *start = (const uint8_t *)(uintptr_t)handles[i];
        for (uint64_t offset = 0; offset < bytes;) {
            if (bytes - offset < sizeof(op_hdr)) rt_fatal("nvn: truncated recorded-fence header");
            op_hdr h;
            memcpy(&h, start + offset, sizeof h);
            if (h.size < 8 || (h.size & 7) || h.size > bytes - offset || h.op >= OP_COUNT_ ||
                h.op == OP_JUMP || h.op == OP_CALL)
                rt_fatal("nvn: corrupt nonflat recording during fence capture");
            if (h.op == OP_FENCE_SYNC) {
                if (h.size < 8 + sizeof(p_fence_sync)) rt_fatal("nvn: short FenceSync record");
                p_fence_sync record;
                memcpy(&record, start + offset + 8, sizeof record);
                fence_parameters(record.condition, record.flags);
                if (!record.sync) rt_fatal("nvn: FenceSync submit has a null output sync");
                sync_t snapshot;
                memcpy(&snapshot, record.sync, sizeof snapshot);
                if (snapshot.magic != MAGIC('S', 'Y', 'N', 'C') ||
                    (snapshot.fenced != 0 && snapshot.fenced != 1 && snapshot.fenced != 3))
                    rt_fatal("nvn: FenceSync submit has an invalid output sync");
                if (!batch) {
                    batch = calloc(1, sizeof *batch);
                    if (!batch) rt_fatal("nvn: recorded fence batch allocation failed");
                }
                fence_list_add(batch, record.sync);
            }
            offset += h.size;
            if (h.op == OP_END) {
                if (offset != bytes) rt_fatal("nvn: data after recording END during fence capture");
                break;
            }
            if (offset == bytes) rt_fatal("nvn: recording lacks END during fence capture");
        }
    }
    return batch;
}

void nvn_submit_publish_fences(NvnFenceBatch *batch, uint64_t fence) {
    if (!batch) return;
    if (!fence) rt_fatal("nvn: backend supplied no completion fence for recorded FenceSync");
    for (size_t i = 0; i < batch->count; i++) {
        // The destination identity was checked before submit. No backend worker
        // retains/dereferences this pointer after QueueSubmitCommands returns.
        __atomic_store_n(&batch->syncs[i]->fence, fence, __ATOMIC_RELEASE);
        __atomic_store_n(&batch->syncs[i]->fenced, 1, __ATOMIC_RELEASE);
    }
    free(batch->syncs);
    free(batch);
}

// Earlier QueueFenceSync, recorded fences in a previous submitted batch, and
// window acquire name work already queued (or complete). Imported producers are
// not implemented. A wait after a fence of the same sync in this batch needs an
// intra-batch split before deferred CPU snapshots, so reject it rather than wait
// for our own not-yet-submitted fence or silently ignore its dependency.
// Waiting for their maximum before the whole batch is conservative: it orders CPU completion callbacks as well
// as GPU work before *any* deferred read or backend snapshot, including those before a wait in another handle.
static uint64_t wait_target(const uint8_t *p, int depth, NvnFenceBatch *produced) {
    uint64_t target = 0;
    for (;;) {
        op_hdr h;
        memcpy(&h, p, sizeof h);
        if (h.size < 8 || (h.size & 7) || h.op >= OP_COUNT_)
            rt_fatal("nvn: corrupt recording at %p while preparing WaitSync (op %u size %u)", (const void *)p, h.op, h.size);
        if (h.op == OP_END) return target;
        if (h.op == OP_JUMP) {
            if (h.size < 16) rt_fatal("nvn: short WaitSync jump record at %p", (const void *)p);
            memcpy(&p, p + 8, 8);
            continue;
        }
        uint64_t value = 0;
        if (h.op == OP_CALL) {
            if (h.size < 16) rt_fatal("nvn: short WaitSync call record at %p", (const void *)p);
            uint64_t called;
            memcpy(&called, p + 8, 8);
            if (depth >= 16) rt_fatal("nvn: WaitSync preparation: call depth over 16");
            value = wait_target((const uint8_t *)(uintptr_t)called, depth + 1, produced);
        } else if (h.op == OP_FENCE_SYNC) {
            if (h.size < 8 + sizeof(p_fence_sync)) rt_fatal("nvn: short FenceSync record during wait preparation");
            const p_fence_sync *fence = (const p_fence_sync *)(const void *)(p + 8);
            fence_list_add(produced, fence->sync);
        } else if (h.op == OP_WAIT_SYNC) {
            if (h.size < 8 + sizeof(p_wait_sync)) rt_fatal("nvn: short WaitSync record at %p", (const void *)p);
            const sync_t *source = ((const p_wait_sync *)(const void *)(p + 8))->sync;
            if (!source) rt_fatal("nvn: CommandBufferWaitSync with a null sync");
            if (fence_list_has(produced, source))
                rt_fatal("nvn: same-submission WaitSync after FenceSync requires an unsupported batch split");
            sync_t snapshot;
            memcpy(&snapshot, source, sizeof snapshot);   // one value-at-submit read; never reread while waiting
            if (snapshot.magic != MAGIC('S', 'Y', 'N', 'C') ||
                (snapshot.fenced != 0 && snapshot.fenced != 1 && snapshot.fenced != 3))
                rt_fatal("nvn: CommandBufferWaitSync with an invalid sync state");
            if (snapshot.fenced) value = snapshot.fence;   // an unfenced SDK sync emits no GPU wait
        }
        if (value > target) target = value;
        p += h.size;
    }
}

static void wait_submission(const uint64_t *handles, int n) {
    uint64_t target = 0;
    NvnFenceBatch produced = {0};
    for (int i = 0; i < n; i++) {
        const copy_prefix *pf = (const copy_prefix *)(uintptr_t)handles[i] - 1;
        if ((pf->sync_ops & COPY_SYNC_KNOWN) && !(pf->sync_ops & (COPY_SYNC_WAIT | COPY_SYNC_FENCE))) continue;
        uint64_t value = wait_target((const uint8_t *)(uintptr_t)handles[i], 0, &produced);
        if (value > target) target = value;
    }
    free(produced.syncs);
    if (!target) return;
    const GfxBackend *b = gfx_backend();
    // No copy/backend lock is held here. QueueSubmitCommands has given its virtual core up. The async front
    // submit lock does not stop an already-enqueued producer: replay/completion never acquire that lock.
    // fence_done/fence_wait include completion callbacks, unlike the raw Vulkan timeline.
    if (b->fence_done() < target && !b->fence_wait(target, UINT64_MAX))
        rt_fatal("nvn: CommandBufferWaitSync could not reach producer fence %llu", (unsigned long long)target);
}

static int resolve_deferred(const uint8_t *p, int depth) {   // returns the number of deferred binds and pushes
    int nd = 0;
    for (;;) {
        op_hdr h;
        memcpy(&h, p, sizeof h);
        if (h.size < 8 || (h.size & 7) || h.op >= OP_COUNT_)
            rt_fatal("nvn: corrupt recording at %p while resolving deferred binds (op %u size %u)", (const void *)p,
                     h.op, h.size);
        if (h.op == OP_END) return nd;
        if (h.op == OP_JUMP) {
            memcpy(&p, p + 8, 8);
            continue;
        }
        if (h.op == OP_CALL) {
            uint64_t t;
            memcpy(&t, p + 8, 8);
            if (depth >= 16) rt_fatal("nvn: deferred bind resolution: call depth over 16");
            nd += resolve_deferred((const uint8_t *)(uintptr_t)t, depth + 1);
        } else if (h.op == OP_PUSH_CONST) {
            if (h.size < 8 + offsetof(p_push_const, w)) rt_fatal("nvn: short push-constant record at %p", (const void *)p);
            p_push_const *b = (p_push_const *)(uintptr_t)(p + 8);
            if (b->src) {
                if (b->bytes <= 0 || b->bytes > (int)sizeof b->w || (b->bytes & 3) ||
                    h.size < 8 + offsetof(p_push_const, w) + (size_t)b->bytes)
                    rt_fatal("nvn: invalid deferred push-constant record at %p", (const void *)p);
                nd++;
                memcpy(b->w, (const void *)(uintptr_t)b->src, (size_t)b->bytes);
            }
        } else if (h.op == OP_BIND_TEX || h.op == OP_BIND_IMG || h.op == OP_BIND_SAMP) {
            NvnOpScratch normalized;
            const void *payload = nvn_op_normalize((const op_hdr *)p, &normalized);
            if (!payload) rt_fatal("nvn: invalid handle-bind record at %p", (const void *)p);
            p_bind_handles *b = (p_bind_handles *)(uintptr_t)payload;
            if (b->src && b->n > 0 && b->n <= 32) {
                nd++;
                uint64_t cur[32];
                memcpy(cur, (const void *)(uintptr_t)b->src, 8 * (size_t)b->n);
                __atomic_fetch_add(&def_reads, 1, __ATOMIC_RELAXED);
                if (memcmp(cur, b->h, 8 * (size_t)b->n)) {
                    if (__atomic_fetch_add(&def_changed, 1, __ATOMIC_RELAXED) < 6)
                        NVN_LOG("deferred bind read new handles at submit: stage %d slot %d, %#llx at record -> %#llx",
                                b->stage, b->first, (unsigned long long)b->h[0], (unsigned long long)cur[0]);
                    memcpy(b->h, cur, 8 * (size_t)b->n);
                }
            }
        }
        p += h.size;
    }
}

void nvn_submit_prepare(const uint64_t *handles, int n) {
    uint64_t now = __atomic_add_fetch(&nsubmits, 1, __ATOMIC_RELAXED);
    nvn_mode_trace(NVN_MODE_PREPARE, 0, 0, (uint32_t)n, now);
    for (int i = 0; i < n; i++) {
        uint64_t handle = handles[i];
        copy_touch(handle, now, "QueueSubmitCommands");
        nvn_mode_trace(NVN_MODE_HANDLE, 0, handle, (uint32_t)i, now);
    }
    wait_submission(handles, n);
    for (int i = 0; i < n; i++) {
        const copy_prefix *pf = (const copy_prefix *)(uintptr_t)handles[i] - 1;
        if (pf->deferred != 1) resolve_deferred((const uint8_t *)(uintptr_t)handles[i], 0);
    }
}

// Asynchronous replay. A copy a queued submission still reads keeps that submission's deferred binds: when it holds
// deferred binds (or is not known to hold none), this submission gets a clone, resolved here and freed after its replay.
void nvn_submit_prepare_async(const uint64_t *handles, uint64_t *out, int n) {
    uint64_t now = __atomic_add_fetch(&nsubmits, 1, __ATOMIC_RELAXED);
    nvn_mode_trace(NVN_MODE_PREPARE, 0, 0, (uint32_t)n, now);
    uint64_t replayed = gfx_replayed_submits();
    plat_mutex_lock(&copy_lock);
    if (nzombies) zombie_sweep(replayed);
    for (int i = 0; i < n; i++) {
        uint64_t handle = handles[i];
        copy_prefix *pf = copy_find(handle);
        if (!pf) {
            plat_mutex_unlock(&copy_lock);
            rt_fatal("nvn: QueueSubmitCommands: command handle %#llx is not a live recording (released or never recorded)",
                     (unsigned long long)handle);
        }
        nvn_mode_trace(NVN_MODE_HANDLE, 0, handle, (uint32_t)i, now);
        out[i] = handle;
        if (pf->used_at > replayed && pf->used_at != now && pf->deferred != 1) {
            size_t sz = sizeof *pf + pf->bytes;
            copy_prefix *cl = malloc(sz);
            if (!cl) rt_fatal("nvn: out of memory (recording clone, %zu bytes)", sz);
            memcpy(cl, pf, sz);
            cl->magic = 0;   // not a live handle: the game never sees it
            cl->used_at = now;
            zombie_add(cl);
            out[i] = (uintptr_t)(cl + 1);
            __atomic_fetch_add(&n_clones, 1, __ATOMIC_RELAXED);
        } else {
            pf->used_at = now;
        }
    }
    plat_mutex_unlock(&copy_lock);
    wait_submission(out, n);
    for (int i = 0; i < n; i++) {
        if (((const copy_prefix *)(uintptr_t)out[i] - 1)->deferred == 1) continue;
        int nd = resolve_deferred((const uint8_t *)(uintptr_t)out[i], 0);
        if (out[i] == handles[i]) ((copy_prefix *)(uintptr_t)out[i] - 1)->deferred = nd ? 2 : 1;
    }
    if (now % 7200 == 0)
        rt_log("nvn: asynchronous replay at submit %llu: %llu recording clones so far, %u copies waiting for the replay",
               (unsigned long long)now, (unsigned long long)__atomic_load_n(&n_clones, __ATOMIC_RELAXED), nzombies);
}

uint64_t nvn_submit_count(void) { return __atomic_load_n(&nsubmits, __ATOMIC_RELAXED); }

int nvn_memory_diagnostics(void) {
    static int enabled = -1;
    static PlatMutex config_lock = PLAT_MUTEX_INIT;
    int value = __atomic_load_n(&enabled, __ATOMIC_ACQUIRE);
    if (value < 0) {
        plat_mutex_lock(&config_lock);
        value = __atomic_load_n(&enabled, __ATOMIC_ACQUIRE);
        if (value < 0) {
            // This diagnostic has no menu default; cfg_bool would require a schema entry.
            const char *setting = cfg_get("TOTK_RUNTIME_MEMORY_DIAG");
            value = setting && !strcmp(setting, "1");
            __atomic_store_n(&enabled, value, __ATOMIC_RELEASE);
        }
        plat_mutex_unlock(&config_lock);
    }
    return value;
}

void nvn_runtime_memory_stats(NvnMemoryStats *out) {
    if (!out) return;
    memset(out, 0, sizeof *out);
    out->record_buffers = __atomic_load_n(&record_buffers, __ATOMIC_RELAXED);
    out->record_bytes = __atomic_load_n(&record_bytes, __ATOMIC_RELAXED);
    out->record_peak_bytes = __atomic_load_n(&record_peak_bytes, __ATOMIC_RELAXED);
    out->record_reinits = __atomic_load_n(&record_reinits, __ATOMIC_RELAXED);
    plat_mutex_lock_shared(&copy_lock);
    out->copies = ncopies;
    out->copy_bytes = copy_bytes + ncopies * sizeof(copy_prefix);
    out->copies_made = copies_made;
    out->copy_index_bytes = copy_bucket_bytes + (uint64_t)copy_pages.cap * (copy_pages.klen + sizeof(void *)) +
                            (uint64_t)copy_live.cap * (copy_live.klen + sizeof(void *)) +
                            (uint64_t)cap_zombies * sizeof *zombies;
    out->zombies = nzombies;
    out->zombie_bytes = zombie_bytes;
    plat_mutex_unlock_shared(&copy_lock);
    plat_mutex_lock_shared(&pin_lock);
    out->texture_pins = pins.n;
    out->texture_pin_bytes = (uint64_t)pins.n * sizeof(tex_t);
    out->texture_pin_index_bytes = (uint64_t)pins.cap * (pins.klen + sizeof(void *));
    plat_mutex_unlock_shared(&pin_lock);
    nvn_program_memory_stats(out);
    NvnReclaimStats reclaim;
    nvn_reclaim_stats(&reclaim);
    out->reclaim_copies = reclaim.copies;
    out->reclaim_bytes = reclaim.bytes;
    out->reclaim_peak_bytes = reclaim.peak_bytes;
}

const char *nvn_op_name(uint32_t op) {
    static const char *const names[] = {
        "END", "JUMP", "BEGIN", "CALL", "SIGNAL_EVENT", "REPORT_COUNTER", "COPY_B2B", "UPDATE_UB", "SET_RT",
        "CLEAR_COLOR", "CLEAR_DS", "VIEWPORTS", "SCISSOR", "DEPTH_RANGE", "BIND_PROGRAM", "BIND_STATE", "BIND_VATTR",
        "BIND_VSTREAM", "BIND_VBUF", "BIND_UBO", "BIND_SSBO", "BIND_TEX", "BIND_SAMP", "STENCIL", "POLY_OFFSET", "DRAW",
        "DRAW_IDX", "DISPATCH", "BARRIER", "SET_TEX_POOL", "SET_SAMP_POOL", "COPY_B2T", "COPY_STRIDE", "BLEND_COLOR",
        "COPY_T2B", "BIND_IMG", "COPY_T2T", "RESET_COUNTER", "CLEAR_TEX", "DEPTH_CLAMP", "RENDER_ENABLE",
        "RASTER_DISCARD", "RENDER_COND", "PRIM_RESTART", "DRAW_IDX_IND", "DISPATCH_IND", "CLEAR_BUF", "PUSH_CONST", "WAIT_SYNC", "FENCE_SYNC", "DRAW_IND", "COPY_IMAGE_STRIDE",
    };
    _Static_assert(sizeof names / sizeof *names == OP_COUNT_, "one name per op");
    return op < OP_COUNT_ ? names[op] : "?";
}
