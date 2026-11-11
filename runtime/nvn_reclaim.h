// CPU-only reclamation of command copies whose replay and public handle lifetime ended.
#pragma once
#include <stdint.h>

typedef struct {
    uint64_t copies, bytes, peak_bytes, batches;
} NvnReclaimStats;

// Success transfers ownership; failure leaves the chain with the caller.
// The callback must only free CPU allocations, never access guest or GPU state.
int nvn_reclaim_submit(void *chain, uint64_t copies, uint64_t bytes, void (*release)(void *));
void nvn_reclaim_stats(NvnReclaimStats *out);
void nvn_reclaim_shutdown(void); // drains and joins; also registered at normal process exit
