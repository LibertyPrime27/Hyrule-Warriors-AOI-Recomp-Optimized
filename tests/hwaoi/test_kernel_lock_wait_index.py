"""Native production wait transitions vs the former full-scan priority calculation.

Only the platform sleep/clock is replaced. No guest bytes, game run, or host
thread scheduling are involved. The extracted functions execute at O0 and O2.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function


def source(mutation=None):
    kernel = (ROOT / "runtime/kernel.c").read_text()
    struct = function(kernel, "struct RtThread {") + ";"
    diagnostics = kernel[kernel.index("typedef struct PriorityDiag"):kernel.index("static int g_priority_diag;") + len("static int g_priority_diag;")]
    enums = kernel[kernel.index("enum { W_NONE"):kernel.index("static const char *const s_name")]
    functions = "\n".join(function(kernel, start) for start in (
        "static void wait_lock_link(", "static void wait_unlink(", "static RtThread *pick(",
        "static void wake(", "static void wake_sync_waiters(", "static uint32_t block_inner(",
        "static int priority_diag_eval(", "static int eff_prio(", "static int prio_of(",
        "static void priority_diag_site(", "static void priority_diag_yield(",
        "static void priority_diag_snapshot(", "static inline uint32_t ld32(",
        "static inline void st32(", "static inline int cas32(", "static void lock_handoff(",
        "static void lock_acquire_for("))
    # Exact SVC case bodies ensure real SetThreadPriority/CancelSynchronization behavior.
    for case, name in (("case 0x0D:", "set_priority"), ("case 0x19:", "cancel_sync")):
        body = function(kernel, case)
        functions += "\nstatic int " + name + "(uint64_t *x) " + body[body.index("{"):]
    if mutation == "missing-conversion":
        start = functions.index("static void lock_acquire_for(")
        functions = functions[:start] + functions[start:].replace("    wait_lock_link(t);", "", 1)
    elif mutation == "stale-exit":
        functions = functions.replace("        t->lock_next = NULL;", "", 1)
    elif mutation == "head-conversion":
        functions = functions.replace("for (RtThread *w = g_wait; w != t; w = w->next)",
                                      "for (RtThread *w = t; w != t; w = w->next)", 1)
    elif mutation == "cached-owner":
        functions = functions.replace(
            "(__atomic_load_n((const uint32_t *)(uintptr_t)w->waddr, __ATOMIC_RELAXED) & ~WAITERS_BIT)",
            "w->wobj", 1)
    preamble = r'''
#include <stdio.h>
#include <stdlib.h>
#include <setjmp.h>
#include "host.h"
#include "kernel.h"
#include "platform/platform.h"
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "check line %d: %s\n", __LINE__, #x); exit(1); } } while (0)
#define R_OK 0u
#define R_TIMEOUT KERN_R_TIMEOUT
#define R_BAD_STATE KERN_R_BAD_STATE
#define R_BAD_HANDLE KERN_R_BAD_HANDLE
#define WAITERS_BIT KERN_WAITERS_BIT
#define O_THREAD 1
#define CRIT_WAKE 1
#define NTHREAD 64
#define NWORD 8
typedef struct Fiber Fiber;
'''
    stubs = r'''
static RtThread threads[NTHREAD], *g_wait, *g_lock_wait, *tcur, *g_main;
static uint32_t words[NWORD];
static uint64_t g_wseq, clock_ns = 1, checks;
static int g_ap = 1, g_crit_on;
static PlatMutex G;
unsigned rt_trace;
static jmp_buf parked;
static int can_park;
static void (*sleep_hook)(RtThread *);
static unsigned sleep_calls, signals;
static void verify(void);
uint64_t kern_now_ns(void) { return clock_ns; }
static void lock(void) {}
static void unlock(void) {}
static void *h_get(uint32_t handle, int type) {
    CHECK(type == O_THREAD);
    for (unsigned i = 0; i < NTHREAD; i++) if (threads[i].handle == handle) return &threads[i];
    return NULL;
}
static void acct(RtThread *t, int st) { t->st = st; }
static void crit_add(int kind, uint32_t a, uint32_t b, int st, uint64_t obj, uint64_t ts) {
    (void)kind; (void)a; (void)b; (void)st; (void)obj; (void)ts;
}
void plat_cond_signal(PlatCond *c) { (void)c; signals++; }
void plat_cond_wait(PlatCond *c, PlatMutex *m) {
    CHECK(m == &G); sleep_calls++;
    RtThread *t = (RtThread *)((char *)c - offsetof(RtThread, cv));
    verify();
    if (sleep_hook) { sleep_hook(t); return; }
    CHECK(can_park); longjmp(parked, 1);
}
int plat_cond_wait_ns(PlatCond *c, PlatMutex *m, uint64_t ns) {
    CHECK(m == &G); sleep_calls++;
    RtThread *t = (RtThread *)((char *)c - offsetof(RtThread, cv));
    verify();
    if (sleep_hook) sleep_hook(t); else clock_ns += ns;
    return 1;
}
'''
    fixture = r'''
// The original production calculation, intentionally kept as a full scan.
static int reference_priority(const RtThread *o) {
    int p = o->prio;
    for (const RtThread *w = g_wait; w; w = w->next)
        if (w->wkind == W_LOCK && w->prio < p && w->waddr &&
            (__atomic_load_n((const uint32_t *)(uintptr_t)w->waddr, __ATOMIC_RELAXED) & ~WAITERS_BIT) == o->handle)
            p = w->prio;
    return p;
}
static void verify(void) {
    unsigned seen[NTHREAD] = {0}, n = 0;
    RtThread *indexed = g_lock_wait;
    for (RtThread *w = g_wait; w; w = w->next) {
        CHECK(++n <= NTHREAD); CHECK(w >= threads && w < threads + NTHREAD);
        CHECK(!seen[w - threads]++); CHECK(w->wkind != W_NONE);
        if (w->wkind == W_LOCK) {
            CHECK(indexed == w); indexed = indexed->lock_next;
        } else CHECK(w->lock_next == NULL);
    }
    CHECK(indexed == NULL);
    int saved = g_ap;
    for (unsigned i = 0; i < NTHREAD; i++) {
        RtThread *t = &threads[i];
        CHECK((t->wkind != W_NONE) == (seen[i] != 0));
        if (!seen[i]) CHECK(t->next == NULL && t->lock_next == NULL);
        int expected = reference_priority(t);
        int saved_diag = g_priority_diag;
        g_priority_diag = 0; CHECK(eff_prio(t) == expected);
        g_priority_diag = 1; CHECK(eff_prio(t) == expected);
        g_priority_diag = saved_diag;
        g_ap = -1; CHECK(prio_of(t) == t->prio);
        g_ap = 0; CHECK(prio_of(t) == t->prio);
        g_ap = 1; CHECK(prio_of(t) == expected);
        g_ap = 2; CHECK(prio_of(t) == expected);
    }
    g_ap = saved; checks++;
}
static void reset(void) {
    memset(threads, 0, sizeof threads); memset(words, 0, sizeof words);
    g_wait = g_lock_wait = tcur = g_main = NULL; g_wseq = 0; clock_ns = 1; sleep_hook = NULL;
    g_priority_diag = 0; memset(g_priority_counts, 0, sizeof g_priority_counts);
    memset(g_priority_sites, 0, sizeof g_priority_sites); memset(g_priority_yields, 0, sizeof g_priority_yields);
    sleep_calls = signals = 0; rt_trace = g_crit_on = 0;
    for (unsigned i = 0; i < NTHREAD; i++) {
        threads[i].handle = 0x1001 + i; threads[i].prio = 59; threads[i].wtag = threads[i].handle;
    }
}
static uint64_t address(unsigned i) { return (uint64_t)(uintptr_t)&words[i]; }
static void park(unsigned i, int kind, unsigned word) {
    RtThread *t = &threads[i]; CHECK(t->wkind == W_NONE);
    // Non-lock addresses deliberately cannot be dereferenced; only a later explicit CV conversion may read a lock.
    t->waddr = kind == W_LOCK ? address(word) : 1;
    t->wkey = 0xabc + word; t->wobj = words[word] & ~WAITERS_BIT;
    unsigned before_sleep = sleep_calls;
    can_park = 1;
    if (!setjmp(parked)) { (void)block_inner(t, kind, -1); CHECK(0); }
    can_park = 0; CHECK(sleep_calls == before_sleep + 1); verify();
}
static void priority(unsigned i, unsigned p) {
    uint64_t x[2] = {threads[i].handle, p}; CHECK(set_priority(x) == 1); CHECK(x[0] == R_OK); verify();
}
static void cancel(unsigned i) {
    uint64_t x[2] = {threads[i].handle, 0}; CHECK(cancel_sync(x) == 1); CHECK(threads[i].cancel); verify();
}
static void mixed(void) {
    reset(); words[0] = threads[0].handle | WAITERS_BIT; words[1] = threads[1].handle;
    for (unsigned i = 4; i < 60; i++) { threads[i].prio = 28 + i % 32; park(i, W_CONDVAR + i % 3, i % NWORD); }
    threads[1].prio = 55; threads[2].prio = 38; threads[3].prio = 29;
    park(2, W_LOCK, 0); park(3, W_LOCK, 1);
    CHECK(eff_prio(&threads[0]) == 38); CHECK(eff_prio(&threads[1]) == 29);
    park(60, W_LOCK, 0); threads[60].waddr = 0; threads[60].prio = 28; verify();
    // Every non-lock wait may carry an invalid address without eff_prio touching it.
    wake_sync_waiters(); verify();
    while (g_wait) { wake(g_wait, 0); verify(); }
}
static void handoff(void) {
    reset(); words[0] = threads[0].handle | WAITERS_BIT;
    threads[1].prio = threads[2].prio = 40; threads[3].prio = 28;
    park(1, W_LOCK, 0); park(2, W_LOCK, 0); park(3, W_LOCK, 0);
    CHECK(eff_prio(&threads[0]) == 28);
    lock_handoff(address(0)); verify(); CHECK(words[0] == (threads[3].handle | WAITERS_BIT));
    CHECK(threads[3].woke && !threads[1].woke && !threads[2].woke);
    CHECK(eff_prio(&threads[0]) == 59);
    lock_handoff(address(0)); verify(); CHECK(words[0] == (threads[1].handle | WAITERS_BIT));
    lock_handoff(address(0)); verify(); CHECK(words[0] == threads[2].handle);
    lock_handoff(address(0)); verify(); CHECK(words[0] == 0);
}
static void dynamic(void) {
    reset(); words[0] = threads[0].handle | WAITERS_BIT;
    threads[2].prio = 40; park(2, W_LOCK, 0);
    CHECK(eff_prio(&threads[0]) == 40);
    st32(address(0), threads[1].handle); verify();
    CHECK(eff_prio(&threads[0]) == 59 && eff_prio(&threads[1]) == 40);
    priority(2, 28); CHECK(eff_prio(&threads[1]) == 28);
    priority(2, 50); CHECK(eff_prio(&threads[1]) == 50);
    priority(1, 30); CHECK(eff_prio(&threads[1]) == 30);
    uint64_t invalid[2] = {threads[2].handle, 27}; CHECK(set_priority(invalid) == 1);
    CHECK(invalid[0] == R_BAD_STATE && threads[2].prio == 50);
    reset(); words[0] = threads[0].handle; words[1] = threads[1].handle;
    threads[0].prio = 56; threads[1].prio = 42; threads[2].prio = 28;
    park(1, W_LOCK, 0); park(2, W_LOCK, 1);
    CHECK(eff_prio(&threads[0]) == 42 && eff_prio(&threads[1]) == 28); // exactly one hop
}
static void conversion(void) {
    reset(); words[0] = threads[0].handle;
    threads[1].prio = threads[3].prio = threads[5].prio = 40;
    park(1, W_LOCK, 0); park(2, W_CONDVAR, 0); park(3, W_LOCK, 0);
    park(4, W_ADDRESS, 0); park(5, W_CONDVAR, 0);
    RtThread *saved[5]; unsigned n = 0;
    for (RtThread *t = g_wait; t; t = t->next) saved[n++] = t;
    uint64_t oldseq = threads[2].wseq;
    lock_acquire_for(&threads[2], address(0), threads[2].handle); verify();
    CHECK(threads[2].wseq > oldseq && threads[2].wkind == W_LOCK);
    n = 0; for (RtThread *t = g_wait; t; t = t->next) CHECK(saved[n++] == t);
    lock_acquire_for(&threads[5], address(0), threads[5].handle); verify();
    CHECK(pick(W_LOCK, address(0)) == &threads[1]); // conversion gets a new FIFO sequence
    park(6, W_CONDVAR, 1); lock_acquire_for(&threads[6], address(1), threads[6].handle); verify();
    CHECK(threads[6].woke && words[1] == threads[6].handle); // immediate success never enters lock index
    wake(&threads[1], 0); verify(); wake(&threads[5], 0); verify();
    park(7, W_CONDVAR, 0); park(8, W_ADDRESS, 0);
    lock_acquire_for(&threads[7], address(0), threads[7].handle); verify();
    while (g_wait) { wake(g_wait, 0); verify(); }
    reset(); words[0] = threads[0].handle;
    park(1, W_CONDVAR, 0); park(2, W_LOCK, 0); park(3, W_SYNC, 0);
    lock_acquire_for(&threads[1], address(0), threads[1].handle); verify(); // tail conversion
}
static void immediate_wake(RtThread *t) { wake(t, 123); verify(); }
static void cv_busy_timeout(RtThread *t) {
    if (t->wkind == W_CONDVAR) { lock_acquire_for(t, address(0), t->handle); verify(); }
    clock_ns += 1; // exercise repeated early/spurious wakeups before deadline
}
static void cv_free_wake(RtThread *t) { lock_acquire_for(t, address(1), t->handle); verify(); }
static void cancelled_wait(RtThread *t) { cancel((unsigned)(t - threads)); }
static void timeout_reentry(void) {
    reset(); words[0] = threads[0].handle;
    RtThread *t = &threads[1];
    for (int kind = W_LOCK; kind <= W_SYNC; kind++) {
        t->waddr = kind == W_LOCK ? address(0) : 1;
        CHECK(block_inner(t, kind, 0) == R_TIMEOUT); verify();
        CHECK(block_inner(t, kind, 1000001) == R_TIMEOUT); verify();
        sleep_hook = immediate_wake;
        CHECK(block_inner(t, kind, -1) == 123); verify();
        clock_ns = UINT64_MAX - 4;
        CHECK(block_inner(t, kind, 10) == 123); verify(); // overflowing deadline is forever
        clock_ns = 1; sleep_hook = NULL;
    }
    sleep_hook = cv_busy_timeout; CHECK(block_inner(t, W_CONDVAR, 4) == R_TIMEOUT); verify();
    CHECK(t->wkind == W_NONE && t->lock_next == NULL);
    sleep_hook = cv_free_wake; CHECK(block_inner(t, W_CONDVAR, -1) == 0); verify();
    sleep_hook = cancelled_wait; CHECK(block_inner(t, W_SYNC, -1) == UINT32_MAX); verify();
    sleep_hook = NULL;
    park(1, W_LOCK, 0); cancel(1); CHECK(threads[1].wkind == W_LOCK);
    park(2, W_CONDVAR, 0); cancel(2); CHECK(threads[2].wkind == W_CONDVAR);
    park(3, W_SYNC, 0); cancel(3); CHECK(threads[3].wkind == W_NONE);
    wake(&threads[1], 0); verify(); park(1, W_ADDRESS, 0); wake(&threads[1], 0); verify();
    park(1, W_LOCK, 0); wake_sync_waiters(); verify(); CHECK(threads[1].wkind == W_LOCK);
}
static void diagnostics_case(void) {
    reset(); words[0] = threads[0].handle | WAITERS_BIT; words[1] = threads[1].handle | WAITERS_BIT;
    threads[2].prio = 30; threads[3].prio = 38;
    park(2, W_LOCK, 0); park(3, W_LOCK, 1); park(4, W_SYNC, 0); park(5, W_ADDRESS, 0);
    memset(g_priority_counts, 0, sizeof g_priority_counts);
    g_main = tcur = &threads[0]; g_priority_diag = 1;
    priority_diag_site(0); priority_diag_site(1); priority_diag_site(2);
    priority_diag_yield(0, 0); priority_diag_yield(-1, 1); priority_diag_yield(-2, 0); priority_diag_yield(-3, 0);
    CHECK(eff_prio(&threads[0]) == 30); CHECK(eff_prio(&threads[1]) == 38);
    CHECK(g_priority_counts[0].calls == 2 && g_priority_counts[0].visits == 4 &&
          g_priority_counts[0].loads == 4 && g_priority_counts[0].donations == 2);
    CHECK(!memcmp(&g_priority_counts[0], &g_priority_counts[1], sizeof(PriorityDiag)));
    tcur = &threads[6]; CHECK(eff_prio(&threads[6]) == 59);
    priority_diag_site(0); priority_diag_yield(-1, 0);
    CHECK(g_priority_counts[0].calls == 3 && g_priority_counts[1].calls == 2);
    char text[1024]; priority_diag_snapshot(text, sizeof text, 120);
    CHECK(strstr(text, "frame 120 calls=3 visits=6 owner_loads=6 donated_queries=2"));
    CHECK(strstr(text, "main_calls=2 main_visits=4 main_owner_loads=4 main_donated_queries=2"));
    CHECK(strstr(text, "waiters=4 lock_waiters=2 unique_mutexes=2 priorities=30:1,38:1"));
    CHECK(strstr(text, "sources_grant_better_kick=2/1/1 main_sources=1/1/1"));
    CHECK(strstr(text, "yield_0_m1_m2_other_give_keep=1/2/1/1/1/4 main_yields=1/1/1/1/1/3"));
    CHECK(!g_priority_counts[0].calls && !g_priority_counts[1].calls);
    g_priority_diag = 0; CHECK(eff_prio(&threads[0]) == 30); CHECK(!g_priority_counts[0].calls);
    priority_diag_snapshot(text, sizeof text, 240); CHECK(!text[0]);
    // Report overflow as a lower bound, never dereference these synthetic addresses.
    RtThread *many = calloc(257, sizeof *many); CHECK(many);
    g_wait = g_lock_wait = many; g_priority_diag = 1;
    for (unsigned i = 0; i < 257; i++) {
        many[i].wkind = W_LOCK; many[i].prio = 40; many[i].waddr = 1 + i;
        many[i].next = many[i].lock_next = i < 256 ? &many[i + 1] : NULL;
    }
    priority_diag_snapshot(text, sizeof text, 360);
    CHECK(strstr(text, "waiters=257 lock_waiters=257 unique_mutexes=256+ priorities=40:257"));
    free(many); g_wait = g_lock_wait = NULL;
}
static uint32_t random_state = 0x1234abcd;
static uint32_t rnd(void) { random_state ^= random_state << 13; random_state ^= random_state >> 17;
    random_state ^= random_state << 5; return random_state; }
static void randomized(void) {
    reset(); for (unsigned i = 0; i < NWORD; i++) words[i] = threads[i].handle | WAITERS_BIT;
    for (unsigned iteration = 0; iteration < 20000; iteration++) {
        unsigned i = rnd() % NTHREAD, word = rnd() % NWORD, action = rnd() % 8;
        RtThread *t = &threads[i];
        switch (action) {
        case 0: if (t->wkind == W_NONE) park(i, W_LOCK + rnd() % 4, word); break;
        case 1: if (t->wkind != W_NONE) wake(t, (uint32_t)-1); break;
        case 2: if (t->wkind == W_CONDVAR) lock_acquire_for(t, address(word), t->handle); break;
        case 3: lock_handoff(address(word)); break;
        case 4: priority(i, 28 + rnd() % 32); break;
        case 5: st32(address(word), rnd() % 5 ? threads[i].handle | WAITERS_BIT : 0); break;
        case 6: cancel(i); break;
        case 7: wake_sync_waiters(); break;
        }
        verify();
    }
    while (g_wait) { wake(g_wait, 0); verify(); }
}
int main(int argc, char **argv) {
    CHECK(argc == 2);
    if (!strcmp(argv[1], "mixed")) mixed();
    else if (!strcmp(argv[1], "handoff")) handoff();
    else if (!strcmp(argv[1], "dynamic")) dynamic();
    else if (!strcmp(argv[1], "conversion")) conversion();
    else if (!strcmp(argv[1], "timeout-reentry")) timeout_reentry();
    else if (!strcmp(argv[1], "randomized")) randomized();
    else if (!strcmp(argv[1], "diagnostics")) diagnostics_case();
    else CHECK(0);
    printf("PASS %s: %llu invariant/differential checks\n", argv[1], (unsigned long long)checks);
    return 0;
}
'''
    return preamble + enums + struct + diagnostics + stubs + functions + fixture


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Existing Windows Clang required")
class KernelLockWaitIndexTests(unittest.TestCase):
    def compile(self, directory, optimization, mutation=None):
        path = Path(directory) / (mutation or "production")
        path.with_suffix(".c").write_text(source(mutation), encoding="utf-8")
        result = subprocess.run([str(CC), optimization, "-Wall", "-Wextra", "-Werror", "-std=c11",
                                 "-I", str(ROOT / "runtime"), str(path.with_suffix(".c")),
                                 "-o", str(path.with_suffix(".exe"))], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return path.with_suffix(".exe")

    def test_wait_transitions_and_full_scan_differential(self):
        for optimization in ("-O0", "-O2"):
            with self.subTest(optimization=optimization), tempfile.TemporaryDirectory(prefix="hwaoi-lock-index-") as tmp:
                exe = self.compile(tmp, optimization)
                for mode in ("mixed", "handoff", "dynamic", "conversion", "timeout-reentry", "randomized", "diagnostics"):
                    with self.subTest(mode=mode):
                        result = subprocess.run([str(exe), mode], capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                        self.assertIn("PASS " + mode, result.stdout)

    def test_negative_controls_detect_missing_or_stale_index_and_cached_owner(self):
        for mutation, mode in (("missing-conversion", "conversion"), ("stale-exit", "mixed"),
                               ("head-conversion", "conversion"), ("cached-owner", "dynamic")):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(prefix="hwaoi-lock-negative-") as tmp:
                exe = self.compile(tmp, "-O2", mutation)
                result = subprocess.run([str(exe), mode], capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("check line", result.stderr)


if __name__ == "__main__":
    unittest.main()
