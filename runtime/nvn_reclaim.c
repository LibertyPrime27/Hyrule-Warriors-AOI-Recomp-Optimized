// Original bounded CPU reclamation queue. Released control-ring recordings can
// hold GiB of malloc blocks; freeing them under the recording index lock stalls
// every recorder. GPU-pending copies remain in nvn_api_cmd.c's zombie list.
#include <stdlib.h>
#include <math.h>
#include "cfg.h"
#include "nvn_reclaim.h"
#include "platform/platform.h"

#define RECLAIM_MAX_BYTES (UINT64_C(2) << 30)
#define RECLAIM_MAX_BATCHES 64
#define RECLAIM_MAX_COPIES (UINT64_C(1) << 20)
typedef struct Batch {
    struct Batch *next;
    void *chain;
    void (*release)(void *);
    uint64_t copies, bytes;
} Batch;
static PlatMutex lock = PLAT_MUTEX_INIT, shutdown_lock = PLAT_MUTEX_INIT;
static PlatCond work = PLAT_COND_INIT;
static Batch *head, *tail;
static PlatThread *worker;
static NvnReclaimStats stats;
static unsigned pending_batches;
static int stopping, registered, mode = -1;

static void reclaim_main(void *unused) {
    (void)unused;
    plat_thread_priority(PLAT_PRIO_BELOW_NORMAL);
    for (;;) {
        plat_mutex_lock(&lock);
        while (!head && !stopping) plat_cond_wait(&work, &lock);
        Batch *batch = head;
        if (!batch) { plat_mutex_unlock(&lock); return; }
        head = batch->next;
        if (!head) tail = NULL;
        plat_mutex_unlock(&lock);
        batch->release(batch->chain);
        plat_mutex_lock(&lock);
        stats.copies -= batch->copies;
        stats.bytes -= batch->bytes;
        pending_batches--;
        plat_mutex_unlock(&lock);
        free(batch);
    }
}

int nvn_reclaim_submit(void *chain, uint64_t copies, uint64_t bytes, void (*release)(void *)) {
    if (!chain || !release || !copies || copies > RECLAIM_MAX_COPIES || !bytes || bytes > RECLAIM_MAX_BYTES) return 0;
    plat_mutex_lock(&lock);
    if (mode < 0) mode = cfg_bool("TOTK_COPY_RECLAIM");
    if (!mode || stopping || pending_batches >= RECLAIM_MAX_BATCHES || stats.bytes > RECLAIM_MAX_BYTES - bytes ||
        stats.copies > RECLAIM_MAX_COPIES - copies) {
        plat_mutex_unlock(&lock); return 0;
    }
    // The chain is already committed; leave room for allocations on the game
    // thread while the worker returns its blocks to the CRT/OS.
    double process_mb = 0, used_mb = 0, limit_mb = 0;
    plat_commit_info(&process_mb, &used_mb, &limit_mb);
    if (!isfinite(limit_mb) || !isfinite(used_mb) || limit_mb <= 0 || used_mb < 0 || limit_mb - used_mb < 128) {
        plat_mutex_unlock(&lock); return 0;
    }
    Batch *batch = malloc(sizeof *batch);
    if (!batch) { plat_mutex_unlock(&lock); return 0; }
    if (!registered) {
        if (atexit(nvn_reclaim_shutdown)) { free(batch); plat_mutex_unlock(&lock); return 0; }
        registered = 1;
    }
    if (!worker) worker = plat_thread_new(reclaim_main, NULL, "command reclaim");
    if (!worker) { free(batch); plat_mutex_unlock(&lock); return 0; }
    *batch = (Batch){.chain = chain, .release = release, .copies = copies, .bytes = bytes};
    if (tail) tail->next = batch; else head = batch;
    tail = batch;
    stats.copies += copies; stats.bytes += bytes; stats.batches++;
    if (stats.bytes > stats.peak_bytes) stats.peak_bytes = stats.bytes;
    pending_batches++;
    plat_cond_signal(&work);
    plat_mutex_unlock(&lock);
    return 1;
}

void nvn_reclaim_stats(NvnReclaimStats *out) {
    if (!out) return;
    plat_mutex_lock(&lock); *out = stats; plat_mutex_unlock(&lock);
}

void nvn_reclaim_shutdown(void) {
    plat_mutex_lock(&shutdown_lock);
    plat_mutex_lock(&lock);
    stopping = 1;
    PlatThread *thread = worker;
    plat_cond_broadcast(&work);
    plat_mutex_unlock(&lock);
    if (thread) plat_thread_join(thread);
    plat_mutex_lock(&lock); worker = NULL; plat_mutex_unlock(&lock);
    plat_mutex_unlock(&shutdown_lock);
}
