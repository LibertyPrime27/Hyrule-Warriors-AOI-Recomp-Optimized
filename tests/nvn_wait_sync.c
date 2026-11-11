// Handwritten NVN commands and a condition-latched completion backend. No game, Vulkan loader or GPU.
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef _WIN32
#include <windows.h>
#else
#include <pthread.h>
#include <sys/mman.h>
#include <unistd.h>
#endif
#include "nvn_api.h"
#include "nvn_reclaim.h"
#include "platform/platform.h"

// Use the actual null replay to check that queued waits never dereference their submit-time source.
#define gfx_backend unchosen_null_backend
#include "../runtime/gfx_null.c"
#undef gfx_backend

#define PROC(n) void nvnp_##n(Cpu *c)
PROC(nvnCommandBufferInitialize);
PROC(nvnCommandBufferFinalize);
PROC(nvnCommandBufferAddCommandMemory);
PROC(nvnCommandBufferAddControlMemory);
PROC(nvnCommandBufferBeginRecording);
PROC(nvnCommandBufferEndRecording);
PROC(nvnCommandBufferGetCommandMemoryUsed);
PROC(nvnCommandBufferWaitSync);
PROC(nvnCommandBufferFenceSync);
PROC(nvnCommandBufferPushGraphicsConstantsDeferred);
PROC(nvnQueueSubmitCommands);
PROC(nvnQueueFenceSync);
PROC(nvnQueueInitialize);
PROC(nvnQueueWaitSync);
PROC(nvnSyncInitialize);
#undef PROC
#define U(p) ((uint64_t)(uintptr_t)(p))
static uint64_t call(void (*fn)(Cpu *), const uint64_t args[8]) {
    Cpu c = {0};
    memcpy(c.x, args, 8 * sizeof *args);
    fn(&c);
    return c.x[0];
}
#define CALL(n, ...) call(nvnp_##n, (const uint64_t[8]){__VA_ARGS__})
static int checks, failures;
#define CHECK(c, text) do { checks++; if (!(c)) { failures++; fprintf(stderr, "FAIL: %s\n", text); } } while (0)

unsigned rt_trace;
// Explicit off diagnostic provider; ordering assertions exercise the real recorder/submit path.
void nvn_mode_trace(unsigned kind, uint64_t cb, uint64_t handle, uint32_t value, uint64_t submit) {
    (void)kind; (void)cb; (void)handle; (void)value; (void)submit;
}
void rt_log(const char *fmt, ...) { (void)fmt; }
uint64_t plat_now_ns(void) {
#ifdef _WIN32
    return GetTickCount64()*1000000ull;
#else
    struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (uint64_t)t.tv_sec*1000000000ull+t.tv_nsec;
#endif
}
int nvn_reclaim_submit(void *p,uint64_t n,uint64_t b,void(*fn)(void*)){(void)p;(void)n;(void)b;(void)fn;return 0;}
void nvn_reclaim_stats(NvnReclaimStats *out){memset(out,0,sizeof *out);}
// This recorder fixture has no guest stack for optional call-site diagnostics.
int plat_mem_readable(uint64_t address, uint64_t size) { (void)address; (void)size; return 0; }
void rt_fatal(const char *fmt, ...) {
    va_list args; va_start(args, fmt); vfprintf(stderr, fmt, args); va_end(args);
    fputc('\n', stderr); exit(70);
}
uint64_t rt_call(Cpu *c, uint64_t fn, int n, ...) {
    (void)c; (void)fn; (void)n; rt_fatal("unexpected guest callback"); return 0;
}
static _Thread_local int core_away_depth;
static uint64_t core_away_calls, core_back_calls;
int kern_core_away(void) {
    core_away_depth++;
    __atomic_fetch_add(&core_away_calls, 1, __ATOMIC_RELAXED);
    return 1;
}
void kern_core_back(int *away) {
    if (*away) {
        if (!core_away_depth) rt_fatal("core restoration without release");
        core_away_depth--;
        __atomic_fetch_add(&core_back_calls, 1, __ATOMIC_RELAXED);
        *away = 0;
    }
}
uint64_t kern_now_ns(void) { return 0; }
uint64_t rt_ticks(void) { return 0; } // synthetic entry clock; this fixture checks ordering, not timestamps
uint64_t rt_ns_to_ticks(uint64_t ns) {
    (void)ns; rt_fatal("unexpected counter report in WaitSync fixture"); return 0;
}
// Unrelated object/observer entry points are linked but must never run in this fixture.
const RtConfig *rt_config(void) { rt_fatal("unexpected configuration lookup"); return NULL; }
const GfxBackend gfx_vulkan_backend = {0};
void nvn_pool_flushed(const pool_t *p, uint64_t off, uint64_t size) {
    (void)p; (void)off; (void)size; rt_fatal("unexpected pool flush");
}
uint64_t rt_original(Cpu *c) { (void)c; rt_fatal("unexpected guest observer"); return 0; }
int cfg_bool(const char *name) { (void)name; rt_fatal("unexpected setting lookup"); return 0; }
const char *cfg_get(const char *name) { (void)name; return NULL; }
#ifdef NVN_RUNTIME_MEMORY_DIAG_VERSION
void nvn_program_memory_stats(NvnMemoryStats *out) { (void)out; }
#endif
void plat_sleep_ms(uint32_t ms) { (void)ms; rt_fatal("unexpected sleep"); }

// Actual concurrent locks, not no-op stubs: the completion thread also acquires the recording-copy lock.
#ifdef _WIN32
_Static_assert(sizeof(PlatMutex) == sizeof(SRWLOCK) && sizeof(PlatCond) == sizeof(CONDITION_VARIABLE), "lock storage");
void plat_mutex_lock(PlatMutex *m) { AcquireSRWLockExclusive((PSRWLOCK)m); }
void plat_mutex_unlock(PlatMutex *m) { ReleaseSRWLockExclusive((PSRWLOCK)m); }
void plat_mutex_lock_shared(PlatMutex *m) { AcquireSRWLockShared((PSRWLOCK)m); }
void plat_mutex_unlock_shared(PlatMutex *m) { ReleaseSRWLockShared((PSRWLOCK)m); }
void plat_cond_wait(PlatCond *c, PlatMutex *m) {
    if (!SleepConditionVariableSRW((PCONDITION_VARIABLE)c, (PSRWLOCK)m, INFINITE, 0)) abort();
}
void plat_cond_broadcast(PlatCond *c) { WakeAllConditionVariable((PCONDITION_VARIABLE)c); }
typedef HANDLE Thread;
#define THREAD_RETURN DWORD WINAPI
#define THREAD_DONE 0
static Thread start(LPTHREAD_START_ROUTINE fn, void *arg) {
    Thread t = CreateThread(NULL, 0, fn, arg, 0, NULL); if (!t) abort(); return t;
}
static void join(Thread t) { WaitForSingleObject(t, INFINITE); CloseHandle(t); }
#else
static void *lazy(void *volatile *slot, int cond) {
    void *p = __atomic_load_n(slot, __ATOMIC_ACQUIRE);
    if (p) return p;
    void *fresh = malloc(cond ? sizeof(pthread_cond_t) : sizeof(pthread_mutex_t));
    if (!fresh) abort();
    if (cond) pthread_cond_init(fresh, NULL); else pthread_mutex_init(fresh, NULL);
    if (__atomic_compare_exchange_n(slot, &p, fresh, 0, __ATOMIC_RELEASE, __ATOMIC_ACQUIRE)) return fresh;
    if (cond) pthread_cond_destroy(fresh); else pthread_mutex_destroy(fresh);
    free(fresh); return p;
}
void plat_mutex_lock(PlatMutex *m) { pthread_mutex_lock(lazy(&m->p, 0)); }
void plat_mutex_unlock(PlatMutex *m) { pthread_mutex_unlock(m->p); }
void plat_mutex_lock_shared(PlatMutex *m) { plat_mutex_lock(m); }
void plat_mutex_unlock_shared(PlatMutex *m) { plat_mutex_unlock(m); }
void plat_cond_wait(PlatCond *c, PlatMutex *m) { pthread_cond_wait(lazy(&c->p, 1), m->p); }
void plat_cond_broadcast(PlatCond *c) { pthread_cond_broadcast(lazy(&c->p, 1)); }
typedef pthread_t Thread;
#define THREAD_RETURN void *
#define THREAD_DONE NULL
static Thread start(void *(*fn)(void *), void *arg) {
    Thread t; if (pthread_create(&t, NULL, fn, arg)) abort(); return t;
}
static void join(Thread t) { pthread_join(t, NULL); }
#endif

static PlatMutex latch = PLAT_MUTEX_INIT;
static PlatCond changed = PLAT_COND_INIT;
static int async_mode, producer_call, callback_release, callback_done, wait_entered, consumer_entered;
static uint64_t issued, gpu_reached, done, replayed, target, waited, producer_no;
static uint32_t source, next_source, captured[2];
static uint64_t batch[2], queued[2];
static int backend_copy_lock_available;
static int queue_wait_failure, queue_wait_returned, cpu_synced_calls;
static int recorded_mode, force_zero_submit;
static sync_t *recorded_sources[2];
static uint64_t backend_sync_values[2];
static uint32_t backend_sync_states[2];
static const p_push_const *push(uint64_t h) {
    for (const op_hdr *op = nvn_op_first(h); op->op != OP_END; op = nvn_op_next(op))
        if (op->op == OP_PUSH_CONST) return nvn_op_payload(op);
    return NULL;
}
int gfx_replay_async(void) { return async_mode; }
uint64_t gfx_replayed_submits(void) {
    plat_mutex_lock(&latch); uint64_t n = replayed; plat_mutex_unlock(&latch); return n;
}
static uint64_t backend_done(void) {
    plat_mutex_lock(&latch); uint64_t n = done; plat_mutex_unlock(&latch); return n;
}
static int backend_wait(uint64_t value, uint64_t timeout) {
    if (timeout != UINT64_MAX) rt_fatal("WaitSync must not invent a timeout");
    if (!core_away_depth) rt_fatal("WaitSync kept the virtual core while blocking");
    plat_mutex_lock(&latch);
    wait_entered = 1; waited = value; plat_cond_broadcast(&changed);
    if (queue_wait_failure) { plat_mutex_unlock(&latch); return 0; }
    while (done < value) plat_cond_wait(&changed, &latch);
    plat_mutex_unlock(&latch);
    return 1;
}
static void backend_cpu_synced(uint64_t value) {
    (void)value;
    cpu_synced_calls++;
}
static uint64_t backend_submit(const uint64_t *handles, int n, uint64_t submitted_ticks) {
    (void)submitted_ticks;
    plat_mutex_lock(&latch);
    uint64_t f = ++issued;
    if (recorded_mode) {
        for (int i = 0; i < 2; i++) {
            backend_sync_values[i] = recorded_sources[i]->fence;
            backend_sync_states[i] = recorded_sources[i]->fenced;
        }
    }
    if (force_zero_submit) { plat_mutex_unlock(&latch); return 0; }
    if (producer_call) {
        producer_no = nvn_submit_count();
        gpu_reached = f;              // GPU signal precedes the deliberately latched CPU completion callback
    } else {
        if (n != 2) abort();
        for (int i = 0; i < 2; i++) {
            queued[i] = handles[i];
            const p_push_const *p = push(handles[i]);
            captured[i] = p ? p->w[0] : 0;
        }
        consumer_entered = 1;
        plat_cond_broadcast(&changed);
    }
    plat_mutex_unlock(&latch);
    return f;
}
static const GfxBackend backend = {.submit=backend_submit, .fence_done=backend_done, .fence_wait=backend_wait,
                                   .cpu_synced=backend_cpu_synced};
const GfxBackend *gfx_backend(void) { return &backend; }

static THREAD_RETURN complete_producer(void *arg) {
    (void)arg;
    plat_mutex_lock(&latch);
    while (!callback_release) plat_cond_wait(&changed, &latch);
    plat_mutex_unlock(&latch);
    // This models an already-enqueued replay/completion path, which never takes the front submit_lock.
    // It must be able to acquire copy_lock while the consumer waits.
    int live = nvn_handle_live(batch[0]);
    plat_mutex_lock(&latch);
    backend_copy_lock_available = live;
    source = next_source;             // producer CPU completion callback
    callback_done = 1;
    replayed = producer_no;
    done = target;                   // publish only after callback, as vk_sync completion_main does
    plat_cond_broadcast(&changed);
    plat_mutex_unlock(&latch);
    return THREAD_DONE;
}
static THREAD_RETURN submit_consumer(void *arg) {
    CALL(nvnQueueSubmitCommands, U(arg), 2, U(batch));
    return THREAD_DONE;
}

static size_t page_size;
static sync_t *sync_page(void) {
#ifdef _WIN32
    SYSTEM_INFO si; GetSystemInfo(&si); page_size = si.dwPageSize;
    void *p = VirtualAlloc(NULL, page_size, MEM_RESERVE|MEM_COMMIT, PAGE_NOACCESS);
    if (!p) abort();
#else
    page_size = (size_t)sysconf(_SC_PAGESIZE);
    void *p = mmap(NULL, page_size, PROT_NONE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) abort();
#endif
    return p;
}
static void protect(sync_t *s, int readable) {
#ifdef _WIN32
    DWORD old; if (!VirtualProtect(s, page_size, readable?PAGE_READWRITE:PAGE_NOACCESS, &old)) abort();
#else
    if (mprotect(s, page_size, readable?(PROT_READ|PROT_WRITE):PROT_NONE)) abort();
#endif
}
static void free_sync(sync_t *s) {
#ifdef _WIN32
    VirtualFree(s, 0, MEM_RELEASE);
#else
    munmap(s, page_size);
#endif
}
static void begin(NVNcommandBuffer *cmd, void *ctl) {
    CALL(nvnCommandBufferInitialize, U(cmd), 0);
    CALL(nvnCommandBufferAddCommandMemory, U(cmd), 0, 0, 0x10000);
    CALL(nvnCommandBufferAddControlMemory, U(cmd), U(ctl), 0x1000);
    CALL(nvnCommandBufferBeginRecording, U(cmd));
}
static void scenario(int async) {
    async_mode = async;
    NVNqueue queue = {0};
    NVNcommandBuffer cmds[2] = {0};
    _Alignas(16) uint8_t control[2][0x1000];
    sync_t *s = sync_page();
    source = 0x11111111;
    // A late wait must precede source reads in *all* handles, even the first handle's push.
    for (int i = 0; i < 2; i++) {
        begin(&cmds[i], control[i]);
        if (i == 1) {
            CALL(nvnCommandBufferWaitSync, U(&cmds[i]), U(s)); // source is NOACCESS
            CHECK(CALL(nvnCommandBufferGetCommandMemoryUsed, U(&cmds[i])) == 0,
                  "CPU control wait does not invent record-time GPU command memory");
        }
        CALL(nvnCommandBufferPushGraphicsConstantsDeferred, U(&cmds[i]), 0, 0, 4, U(&source));
        batch[i] = CALL(nvnCommandBufferEndRecording, U(&cmds[i]));
    }
    protect(s, 1);
    CALL(nvnSyncInitialize, U(s), 0);
    uint64_t previous_queued[2] = {0};
    uint32_t previous_value = 0;
    for (int round = 0; round < 2; round++) {
        // Real QueueFenceSync associates the sync with the backend value returned for this producer.
        producer_call = 1;
        CALL(nvnQueueSubmitCommands, U(&queue), 0, 0);
        CALL(nvnQueueFenceSync, U(&queue), U(s), NVN_SYNC_CONDITION_ALL_GPU_COMMANDS_COMPLETE, 0);
        producer_call = 0;
        target = s->fence;
        uint32_t before[2] = {push(batch[0])->w[0], push(batch[1])->w[0]};
        plat_mutex_lock(&latch);
        callback_release = callback_done = wait_entered = consumer_entered = backend_copy_lock_available = 0;
        waited = 0;
        next_source = 0x22222222u + (unsigned)round;
        plat_mutex_unlock(&latch);
        Thread completion = start(complete_producer, NULL);
        Thread consumer = start(submit_consumer, &queue);
        plat_mutex_lock(&latch);
        while (!wait_entered && !consumer_entered) plat_cond_wait(&changed, &latch);
        CHECK(gpu_reached >= target && done < target, "GPU timeline is reached while host completion is still pending");
        CHECK(wait_entered && waited == target, "wait reads the current QueueFenceSync value at submit");
        CHECK(!consumer_entered && !callback_done, "raw GPU reach does not release consumer before CPU callback");
        CHECK(push(batch[0])->w[0] == before[0] && push(batch[1])->w[0] == before[1],
              "no deferred snapshot in an earlier handle before the late wait");
        callback_release = 1;
        plat_cond_broadcast(&changed);
        plat_mutex_unlock(&latch);
        join(completion); join(consumer);
        CHECK(backend_copy_lock_available, "producer completion can acquire copy_lock while consumer waits");
        CHECK(consumer_entered && captured[0] == next_source && captured[1] == next_source,
              "all consumer snapshots observe completed producer writes");
        if (round && async) {
            CHECK(queued[0] != previous_queued[0], "pending asynchronous resubmit clones deferred data");
            CHECK(push(previous_queued[0])->w[0] == previous_value, "earlier queued snapshot stays immutable");
        }
        previous_queued[0] = queued[0]; previous_queued[1] = queued[1];
        previous_value = captured[0];
        protect(s, 0);
        gfx_null_backend.submit(queued, 2, 0); // actual backend traversal must never reread the pointer
        protect(s, 1);
    }
    // An initialized but unfenced sync emits no SDK wait; window acquire's fence0 is already complete.
    for (int acquired = 0; acquired < 2; acquired++) {
        CALL(nvnSyncInitialize, U(s), 0);
        if (acquired) s->fenced = 3;
        wait_entered = 0;
        source = 0x33333333;
        if (async) nvn_submit_prepare_async(batch, queued, 2); else nvn_submit_prepare(batch, 2);
        CHECK(!wait_entered, "unfenced and acquired syncs need no backend wait");
        CHECK(push(async?queued[0]:batch[0])->w[0] == source, "satisfied wait still refreshes deferred data");
    }
    plat_mutex_lock(&latch); replayed = nvn_submit_count(); plat_mutex_unlock(&latch);
    for (int i = 0; i < 2; i++) {
        CALL(nvnCommandBufferAddControlMemory, U(&cmds[i]), U(control[i]), sizeof control[i]);
        CALL(nvnCommandBufferFinalize, U(&cmds[i]));
    }
    free_sync(s);
}
typedef struct { NVNqueue *queue; sync_t *sync; uint64_t result; } QueueWaitJob;
static THREAD_RETURN queue_wait_consumer(void *arg) {
    QueueWaitJob *job = arg;
    uint64_t result = CALL(nvnQueueWaitSync, U(job->queue), U(job->sync));
    plat_mutex_lock(&latch);
    job->result = result;
    queue_wait_returned = 1;
    plat_cond_broadcast(&changed);
    plat_mutex_unlock(&latch);
    return THREAD_DONE;
}

static void queue_wait_scenario(void) {
    NVNqueue queue = {0};
    queue_builder_t builder = {0};
    CALL(nvnQueueInitialize, U(&queue), U(&builder));
    sync_t *s = sync_page();
    protect(s, 1);
    CALL(nvnSyncInitialize, U(s), 0);
    wait_entered = queue_wait_failure = cpu_synced_calls = 0;
    done = 41;
    uint64_t away_before = __atomic_load_n(&core_away_calls, __ATOMIC_RELAXED);
    s->fence = 99; // unfenced contents are not a producer; match the supported recorder semantics
    CHECK(CALL(nvnQueueWaitSync, U(&queue), U(s)) == 1, "QueueWaitSync accepts unfenced sync without a wait");
    s->fenced = 3;
    s->fence = 0;
    CHECK(CALL(nvnQueueWaitSync, U(&queue), U(s)) == 1, "QueueWaitSync accepts a completed window acquire");
    s->fenced = 1;
    s->fence = 41;
    CHECK(CALL(nvnQueueWaitSync, U(&queue), U(s)) == 1, "QueueWaitSync accepts an already-complete producer");
    CHECK(!wait_entered && core_away_calls == away_before, "satisfied queue waits never enter the blocking backend");
    CHECK(cpu_synced_calls == 0, "GPU queue wait does not request client-memory writeback");

    s->fence = 42;
    queue_wait_returned = wait_entered = 0;
    waited = 0;
    QueueWaitJob job = {&queue, s, UINT64_MAX};
    Thread consumer = start(queue_wait_consumer, &job);
    plat_mutex_lock(&latch);
    while (!wait_entered) plat_cond_wait(&changed, &latch);
    CHECK(waited == 42 && !queue_wait_returned && done == 41, "pending queue wait blocks on the producer frontier");
    // The call already captured its source. A later rewrite and revocation of
    // access must not alter the wait target or cause a second pointer read.
    s->fenced = 0;
    s->fence = 999;
    protect(s, 0);
    done = 42;
    plat_cond_broadcast(&changed);
    plat_mutex_unlock(&latch);
    join(consumer);
    CHECK(job.result == 1 && queue_wait_returned && waited == 42, "queue wait uses its immutable call-time snapshot");
    CHECK(core_away_calls == away_before + 1 && core_back_calls == core_away_calls,
          "delayed queue wait releases and restores the virtual core exactly once");
    protect(s, 1);

    s->fenced = 1;
    s->fence = 84;
    done = 83;
    queue_wait_failure = 1;
    wait_entered = 0;
    CHECK(CALL(nvnQueueWaitSync, U(&queue), U(s)) == 0, "backend wait failure returns NVNfalse, not success");
    CHECK(wait_entered && waited == 84 && !core_away_depth && core_back_calls == core_away_calls,
          "failed wait still restores its virtual core");
    CHECK(cpu_synced_calls == 0, "neither successful nor failed queue waits call cpu_synced");
    queue_wait_failure = 0;
    free_sync(s);
}

static void recorded_fence_scenario(int async) {
    async_mode = async;
    recorded_mode = 0;
    NVNqueue queue = {0};
    queue_builder_t builder = {0};
    CALL(nvnQueueInitialize, U(&queue), U(&builder));
    NVNcommandBuffer cmds[2] = {0};
    _Alignas(16) uint8_t control[2][0x1000];
    sync_t *a = sync_page(), *b = sync_page(); // NOACCESS throughout recording
    begin(&cmds[0], control[0]);
    CALL(nvnCommandBufferWaitSync, U(&cmds[0]), U(a)); // old producer; wait precedes this batch's new fence
    CALL(nvnCommandBufferFenceSync, U(&cmds[0]), U(a), NVN_SYNC_CONDITION_ALL_GPU_COMMANDS_COMPLETE, 0);
    batch[0] = CALL(nvnCommandBufferEndRecording, U(&cmds[0]));
    begin(&cmds[1], control[1]);
    CALL(nvnCommandBufferFenceSync, U(&cmds[1]), U(b), NVN_SYNC_CONDITION_GRAPHICS_WORLD_SPACE_COMPLETE, 0);
    batch[1] = CALL(nvnCommandBufferEndRecording, U(&cmds[1]));
    CHECK(nvn_op_first(batch[1])->op == OP_BEGIN, "FenceSync is a real retained recording, not an immediate call");
    protect(a, 1); protect(b, 1);
    CALL(nvnSyncInitialize, U(a), 0);
    CALL(nvnSyncInitialize, U(b), 0);
    a->fenced = 1; a->fence = 105;
    done = 100; issued = 120;
    producer_call = 0;
    recorded_sources[0] = a; recorded_sources[1] = b;
    recorded_mode = 1;
    wait_entered = consumer_entered = 0;
    waited = 0;
    Thread submit = start(submit_consumer, &queue);
    plat_mutex_lock(&latch);
    while (!wait_entered && !consumer_entered) plat_cond_wait(&changed, &latch);
    CHECK(wait_entered && waited == 105 && !consumer_entered, "wait-before-fence waits on the old submitted producer");
    CHECK(a->fence == 105 && a->fenced == 1 && b->fenced == 0, "no recorded destination is published before backend submit");
    a->fence = 777; // the previously captured wait still targets 105, not a later CPU rewrite
    done = 105;
    plat_cond_broadcast(&changed);
    plat_mutex_unlock(&latch);
    join(submit);
    CHECK(backend_sync_values[0] == 777 && backend_sync_states[0] == 1 && backend_sync_states[1] == 0,
          "the backend receives the batch before recorded fences are published");
    CHECK(a->fenced == 1 && b->fenced == 1 && a->fence == 121 && b->fence == 121,
          "both condition0 and condition1 receive the whole-batch completion frontier");
    CHECK(done == 105 && done < a->fence, "publishing a fence does not fabricate GPU completion");

    QueueWaitJob job = {&queue, a, UINT64_MAX};
    wait_entered = queue_wait_returned = 0;
    Thread consumer = start(queue_wait_consumer, &job);
    plat_mutex_lock(&latch);
    while (!wait_entered) plat_cond_wait(&changed, &latch);
    CHECK(waited == 121 && !queue_wait_returned, "QueueWaitSync waits for the recorded producer's actual completion");
    done = 121;
    plat_cond_broadcast(&changed);
    plat_mutex_unlock(&latch);
    join(consumer);
    CHECK(job.result == 1, "recorded fence releases its waiter only after the completion frontier advances");
    protect(a, 0); protect(b, 0);
    gfx_null_backend.submit(queued, 2, 0); // replay must not dereference a recorded output after the submit call
    protect(a, 1); protect(b, 1);

    CALL(nvnQueueSubmitCommands, U(&queue), 2, U(batch));
    CHECK(a->fence == 122 && b->fence == 122, "resubmitting the same recording publishes a new fence value");
    CHECK(cpu_synced_calls == 0, "recorded fences and GPU waits do not misuse CPU visibility hooks");
    recorded_mode = 0;
    plat_mutex_lock(&latch); replayed = nvn_submit_count(); plat_mutex_unlock(&latch);
    for (int i = 0; i < 2; i++) {
        CALL(nvnCommandBufferAddControlMemory, U(&cmds[i]), U(control[i]), sizeof control[i]);
        CALL(nvnCommandBufferFinalize, U(&cmds[i]));
    }
    free_sync(a); free_sync(b);
}

static void invalid_recorded_fence(const char *kind) {
    NVNqueue queue = {0};
    queue_builder_t builder = {0};
    NVNcommandBuffer cmds[2] = {0};
    _Alignas(16) uint8_t control[2][0x1000];
    sync_t sync;
    CALL(nvnQueueInitialize, U(&queue), U(&builder));
    CALL(nvnSyncInitialize, U(&sync), 0);
    begin(&cmds[0], control[0]);
    if (!strcmp(kind, "--fence-flags"))
        CALL(nvnCommandBufferFenceSync, U(&cmds[0]), U(&sync), 0, 1);
    else if (!strcmp(kind, "--fence-condition"))
        CALL(nvnCommandBufferFenceSync, U(&cmds[0]), U(&sync), 2, 0);
    else if (!strcmp(kind, "--fence-null"))
        CALL(nvnCommandBufferFenceSync, U(&cmds[0]), 0, 0, 0);
    else {
        CALL(nvnCommandBufferFenceSync, U(&cmds[0]), U(&sync), 0, 0);
        if (!strcmp(kind, "--fence-cycle")) CALL(nvnCommandBufferWaitSync, U(&cmds[0]), U(&sync));
        batch[0] = CALL(nvnCommandBufferEndRecording, U(&cmds[0]));
        int n = 1;
        if (!strcmp(kind, "--fence-cross-cycle")) {
            begin(&cmds[1], control[1]);
            CALL(nvnCommandBufferWaitSync, U(&cmds[1]), U(&sync));
            batch[1] = CALL(nvnCommandBufferEndRecording, U(&cmds[1]));
            n = 2;
        } else if (!strcmp(kind, "--fence-invalid-sync")) sync.magic = 0;
        else if (!strcmp(kind, "--fence-zero-submit")) force_zero_submit = 1;
        producer_call = 1;
        CALL(nvnQueueSubmitCommands, U(&queue), n, U(batch));
    }
    rt_fatal("invalid recorded FenceSync was silently accepted");
}

static void invalid_queue_wait(const char *kind) {
    NVNqueue queue = {0};
    queue_builder_t builder = {0};
    sync_t sync;
    CALL(nvnQueueInitialize, U(&queue), U(&builder));
    CALL(nvnSyncInitialize, U(&sync), 0);
    if (!strcmp(kind, "--null-queue")) CALL(nvnQueueWaitSync, 0, U(&sync));
    else if (!strcmp(kind, "--bad-queue")) {
        ((queue_t *)&queue)->magic = 0;
        CALL(nvnQueueWaitSync, U(&queue), U(&sync));
    } else if (!strcmp(kind, "--null-sync")) CALL(nvnQueueWaitSync, U(&queue), 0);
    else if (!strcmp(kind, "--bad-sync")) {
        sync.magic = 0;
        CALL(nvnQueueWaitSync, U(&queue), U(&sync));
    } else if (!strcmp(kind, "--bad-state")) {
        sync.fenced = 2;
        CALL(nvnQueueWaitSync, U(&queue), U(&sync));
    } else if (!strcmp(kind, "--bad-acquire")) {
        sync.fenced = 3; sync.fence = 4;
        CALL(nvnQueueWaitSync, U(&queue), U(&sync));
    } else rt_fatal("unknown fixture argument");
    rt_fatal("invalid QueueWaitSync was silently accepted");
}

int main(int argc, char **argv) {
    if (argc == 2) {
        if (!strncmp(argv[1], "--fence-", 8)) invalid_recorded_fence(argv[1]);
        else invalid_queue_wait(argv[1]);
        return 1;
    }
    scenario(0);
    scenario(1);
    queue_wait_scenario();
    recorded_fence_scenario(0);
    recorded_fence_scenario(1);
    printf("WaitSync: %d checks, %d failures\n", checks, failures);
    return failures != 0;
}
