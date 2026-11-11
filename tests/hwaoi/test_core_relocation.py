"""Exercise real virtual-core eligibility and safe-point handoff without a ROM.

The fixture extracts the production scheduler functions. Only platform waits,
clock/interrupt APIs, accounting and the priority-inheritance oracle are mocked.
It fails on an unexpected blocking wait; successful relocation runs the actual
core_grant/core_put/core_take path, not a second scheduler implementation.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")


def function(source, signature):
    begin = source.index(signature)
    while ";" in source[begin:source.index("{", begin)]:
        begin = source.index(signature, begin + len(signature))
    end = source.index("{", begin) + 1
    depth = 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[begin:end] + "\n"


HEAD = r'''
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#define NCORE 6
#define RT_TRACE_SCHED 1
#define AP_FIRST_NS 100
#define AP_GAP_NS 100
#define AP_LOG_N 10
enum { S_RUN, S_CORE, DG_GAME, DG_HOST, AP_NONE, AP_RETRY, AP_SENT };
typedef struct Cpu { uint64_t pc, x[31]; } Cpu;
typedef struct RtThread RtThread;
struct DgRel { uint64_t kick, host_end, pc, lr; int host, pre, prio; uint32_t h; };
struct RtThread {
    int preempt, host_depth, prio, effective, core, cur_core;
    uint32_t handle, tid;
    uint64_t mask, cseq, pre_since, npre, grant_at, lg_ns, lg_n, lg_max;
    uint64_t dg_since, dg_kick, dg_host_end, dg_pc, dg_lr;
    int dg_host_kick, dg_cls, cv;
    struct DgRel dg_rel;
    RtThread *cnext, *target;
    Cpu *cur;
    uint8_t tlr[0x200];
    void *hthr;
    int ap_pending;
    uint64_t ap_last, ap_rip;
};
static RtThread *g_core_owner[NCORE], *g_core_wait, *tcur;
static struct DgRel g_dg_rel;
static int g_held_max, g_diag, rt_trace, g_ap = 1, g_ap_int, G;
static uint64_t g_wseq, g_pre_n, g_pre_ns, g_pre_max, g_pre_req, g_pre_stale;
static uint64_t g_ap_spin_ok, g_ap_host, g_ap_deferred, g_ap_fail, g_ap_pc, g_ap_sent, g_ap_taken;
static int g_ap_log;
static uint64_t g_gen_lo = 0x1000, g_gen_hi = 0x2000;
static uint64_t now = 100000, stop_rip = 0x1500;
static int lock_depth, signals, stop_calls, resume_calls, stop_result = 1, resume_result = 1;
static uint64_t resume_to;
static RtThread *host_transition;
static int checks;
#define CHECK(c) do { checks++; if (!(c)) { fprintf(stderr, "line %d: %s\n", __LINE__, #c); abort(); } } while (0)
static uint64_t kern_now_ns(void) { return ++now; }
static void lock(void) { CHECK(!lock_depth); lock_depth++; }
static void unlock(void) { CHECK(lock_depth == 1); lock_depth--; }
static int eff_prio(const RtThread *t) { return t->effective; }
static void priority_diag_site(unsigned site) { (void)site; }
static int cores_enabled(void) { return 1; }
static void acct(RtThread *t, int st) { (void)t; (void)st; }
static void plat_cond_signal(int *cv) { (*cv)++; signals++; }
static void plat_cond_wait(int *cv, int *mutex) { (void)cv; (void)mutex; CHECK(0 && "unexpected scheduler block"); }
static void plat_cond_wait_ms(int *cv, int *mutex, uint32_t ms) { (void)ms; plat_cond_wait(cv, mutex); }
static void plat_pause(void) { }
static int dg_class0(RtThread *t) { (void)t; return 0; }
static void dg_done(RtThread *t) { (void)t; }
static void dg_sample(RtThread *t) { (void)t; }
static int dg_in_host(const RtThread *t) { return t->host_depth > 0; }
static void rt_log(const char *format, ...) { (void)format; }
static void rt_fatal(const char *format, ...) { (void)format; abort(); }
static uintptr_t plat_exe_base(void) { return 0; }
static int plat_intr_stop(void *thread, uint64_t *rip, uint64_t *rsp) {
    (void)thread; stop_calls++; *rip = stop_rip; *rsp = 0x3000;
    if (host_transition) host_transition->host_depth = 1;
    return stop_result;
}
static int plat_intr_resume(void *thread, uint64_t to) {
    (void)thread; resume_calls++; resume_to = to; return resume_result;
}
static void kern_ap_stub(void) { }
static void core_kick(void);
'''

TAIL = r'''
static RtThread owner, waiter, third, other[NCORE];
static void reset(void) {
    memset(g_core_owner, 0, sizeof g_core_owner);
    memset(&owner, 0, sizeof owner); memset(&waiter, 0, sizeof waiter);
    memset(&third, 0, sizeof third); memset(other, 0, sizeof other);
    owner.prio = owner.effective = waiter.prio = waiter.effective = third.prio = third.effective = 44;
    owner.mask = 0x3f; waiter.mask = third.mask = 1;
    owner.cur_core = owner.core = 0; waiter.cur_core = third.cur_core = -1;
    owner.hthr = &owner; owner.handle = 0x108d; waiter.handle = 0x1001;
    owner.cseq = 0; waiter.cseq = 1; third.cseq = 2;
    g_core_owner[0] = &owner; g_core_wait = &waiter; tcur = &owner;
    g_diag = rt_trace = g_ap_int = lock_depth = signals = stop_calls = resume_calls = 0;
    g_ap = stop_result = resume_result = 1; resume_to = 0; host_transition = NULL;
    g_pre_n = g_pre_ns = g_pre_max = g_pre_req = g_pre_stale = 0;
    g_ap_deferred = g_ap_host = g_ap_fail = g_ap_pc = g_ap_sent = g_ap_taken = 0;
    stop_rip = 0x1500;
}
static void request(void) { lock(); core_kick(); unlock(); }
static void full(void) {
    for (int c = 1; c < NCORE; c++) { other[c].prio = other[c].effective = 44; other[c].mask = 1ull << c; other[c].cur_core = c; g_core_owner[c] = &other[c]; }
}
int main(void) {
    // Exhaust all six-core affinity/occupancy combinations. Specification:
    // the origin does not count; only a different idle permitted core counts.
    for (unsigned mask = 0; mask < 64; mask++) for (unsigned busy = 0; busy < 64; busy++) for (int from = 0; from < 6; from++) {
        reset(); owner.mask = mask;
        for (int c = 0; c < 6; c++) g_core_owner[c] = busy & (1u << c) ? &other[c] : NULL;
        unsigned available = mask & ~busy & 63u & ~(1u << from);
        CHECK(core_can_relocate(&owner, from) == !!available);
    }
    reset(); request();
    CHECK(owner.preempt && waiter.target == &owner && owner.cur_core == 0 && waiter.cur_core == -1);
    kern_checkpoint();
    CHECK(waiter.cur_core == 0 && owner.cur_core == 1 && g_core_owner[0] == &waiter && g_core_owner[1] == &owner);
    CHECK(!owner.preempt && !g_core_wait && signals == 2 && waiter.cv == 2 && !lock_depth);
    reset(); rt_trace = RT_TRACE_SCHED; g_diag = 1; request(); CHECK(g_pre_req == 1); kern_checkpoint();
    CHECK(g_pre_n == 1 && g_pre_ns > 0 && waiter.cur_core == 0 && owner.cur_core == 1);
    // A different mask must put the owner on core 5, not an arbitrary free core.
    reset(); owner.mask = 0x21; request(); kern_checkpoint(); CHECK(owner.cur_core == 5 && waiter.cur_core == 0);
    reset(); owner.mask = 1; request(); CHECK(!owner.preempt && !waiter.target && !core_better_waiter(&owner, 0));
    reset(); owner.mask = 0x41; request(); CHECK(!owner.preempt && !core_can_relocate(&owner, 0));
    reset(); full(); request(); CHECK(!owner.preempt && !core_better_waiter(&owner, 0));
    // Capacity can appear after the pinned waiter entered an untimed wait.
    // Releasing core1 cannot directly grant mask1, but must request relocation
    // from core0 and wake the waiter so async-preemption retries can start.
    reset(); full(); request(); CHECK(!waiter.target && !owner.preempt && waiter.cv == 0);
    lock(); core_put(&other[1]); unlock();
    CHECK(waiter.target == &owner && owner.preempt && waiter.cv == 1 && waiter.cur_core == -1);
    request(); CHECK(waiter.cv == 1); // unchanged outstanding request is not signalled repeatedly
    kern_checkpoint(); CHECK(waiter.cur_core == 0 && owner.cur_core == 1 && waiter.cv == 2);
    // Same target, previously requested then dropped after its spare was
    // occupied: a later free core must renew both request and wakeup.
    reset(); request(); CHECK(waiter.cv == 1); full(); kern_checkpoint();
    CHECK(waiter.target == &owner && !owner.preempt && waiter.cur_core == -1);
    lock(); core_put(&other[1]); unlock();
    CHECK(waiter.target == &owner && owner.preempt && waiter.cv == 2);
    kern_checkpoint(); CHECK(waiter.cur_core == 0 && owner.cur_core == 1 && waiter.cv == 3);
    reset(); full(); owner.mask = 1; request(); lock(); core_put(&other[1]); unlock();
    CHECK(!owner.preempt && !waiter.target && waiter.cv == 0);
    reset(); waiter.mask = 2; request(); CHECK(!owner.preempt && !core_better_waiter(&owner, 0));
    reset(); waiter.effective = 45; request(); CHECK(!owner.preempt && !core_better_waiter(&owner, 0));
    // Existing strict-priority preemption works even without relocation space.
    reset(); full(); waiter.effective = 43; request(); CHECK(owner.preempt && core_better_waiter(&owner, 0));
    reset(); owner.effective = 30; request(); CHECK(!owner.preempt && !core_better_waiter(&owner, 0));
    reset(); owner.prio = 59; owner.effective = 44; request(); CHECK(owner.preempt);
    // A request is advisory: capacity and waiter liveness are checked again.
    reset(); request(); full(); kern_checkpoint(); CHECK(owner.cur_core == 0 && waiter.cur_core == -1 && !owner.preempt && g_pre_stale == 1);
    reset(); request(); g_core_wait = NULL; kern_checkpoint(); CHECK(owner.cur_core == 0 && !owner.preempt && g_pre_stale == 1);
    reset(); request(); owner.mask = 1; kern_checkpoint(); CHECK(owner.cur_core == 0 && !owner.preempt);
    reset(); request(); waiter.effective = 45; kern_checkpoint(); CHECK(owner.cur_core == 0 && !owner.preempt);
    // Two owners can initially see the same spare core. Only the first may
    // consume it; the other's request must become stale at its safe point.
    reset(); owner.mask = 5; third.mask = 2; waiter.cnext = &third;
    other[1].prio = other[1].effective = 44; other[1].mask = 6;
    other[1].core = other[1].cur_core = 1; g_core_owner[1] = &other[1];
    request(); CHECK(owner.preempt && other[1].preempt);
    kern_checkpoint(); CHECK(owner.cur_core == 2 && waiter.cur_core == 0);
    tcur = &other[1]; kern_checkpoint();
    CHECK(other[1].cur_core == 1 && third.cur_core == -1 && !other[1].preempt && g_pre_stale == 1);
    // Deferred kernel switching retains guest disable/interrupt-flag semantics.
    reset(); ap_tlr(&owner)[0] = 1; request(); kern_checkpoint();
    CHECK(owner.cur_core == 0 && waiter.cur_core == -1 && ap_tlr(&owner)[1] == 1 && !owner.preempt && g_ap_deferred == 1);
    ap_tlr(&owner)[0] = 0; ap_sync(&owner); CHECK(owner.cur_core == 1 && waiter.cur_core == 0);
    // Grant order remains priority then queue sequence, not linked-list order.
    reset(); waiter.cnext = &third; third.effective = 43; request(); kern_checkpoint();
    CHECK(third.cur_core == 0 && owner.cur_core == 1 && g_core_wait == &waiter);
    reset(); waiter.cnext = &third; third.cseq = 0; request(); kern_checkpoint();
    CHECK(third.cur_core == 0 && owner.cur_core == 1 && g_core_wait == &waiter);
    // Actual async interrupt gate: host code and disable counts forbid redirect.
    reset(); request(); owner.host_depth = 1; CHECK(ap_try(&waiter) == AP_RETRY && stop_calls == 0 && g_ap_host == 1);
    reset(); request(); ap_tlr(&owner)[0] = 1; CHECK(ap_try(&waiter) == AP_RETRY && !stop_calls && ap_tlr(&owner)[1] == 1);
    reset(); request(); host_transition = &owner; CHECK(ap_try(&waiter) == AP_RETRY && stop_calls == 1 && resume_calls == 1 && resume_to == 0);
    reset(); request(); stop_rip = 0x3000; CHECK(ap_try(&waiter) == AP_RETRY && resume_to == 0 && g_ap_pc == 1);
    reset(); request(); stop_result = 0; CHECK(ap_try(&waiter) == AP_RETRY && !resume_calls && g_ap_fail == 1);
    reset(); request(); CHECK(ap_try(&waiter) == AP_SENT && owner.ap_pending && resume_calls == 1 && resume_to != 0);
    kern_ap_yield(); CHECK(!owner.ap_pending && owner.cur_core == 1 && waiter.cur_core == 0 && g_ap_taken == 1);
    reset(); request(); CHECK(ap_try(&waiter) == AP_SENT); full(); kern_ap_yield();
    CHECK(owner.cur_core == 0 && waiter.cur_core == -1 && !owner.ap_pending && !g_ap_taken);
    CHECK(g_ap_spin_ok == 0);
    printf("core relocation: %d checks passed\n", checks);
    return 0;
}
'''


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Existing Windows Clang required")
class CoreRelocationTests(unittest.TestCase):
    def test_affinity_priority_stale_requests_and_safe_point_progress(self):
        kernel = (ROOT / "runtime/kernel.c").read_text(encoding="utf-8")
        # Preserve source attribution in the temporary native extraction.
        source = "\n".join(kernel.splitlines()[:6]) + "\n" + HEAD
        for signature in (
            "static int core_allowed(", "static int prio_of(", "static volatile uint16_t *ap_tlr(",
            "static int ap_disabled(", "static int ap_try(", "static void core_count(",
            "static void core_grant(", "static void core_put(", "static int core_can_relocate(",
            "static int core_better_waiter(", "static void core_kick(", "static void core_take(",
            "void kern_checkpoint(", "void kern_ap_yield(", "static void ap_sync(",
        ):
            source += function(kernel, signature)
        source += TAIL
        with tempfile.TemporaryDirectory(prefix="core-relocation-") as temporary:
            path = Path(temporary) / "fixture.c"
            path.write_text(source, encoding="utf-8")
            for optimization in ("0", "2"):
                with self.subTest(optimization=optimization):
                    exe = path.with_name(f"fixture-{optimization}.exe")
                    build = subprocess.run([str(CC), "-std=gnu11", f"-O{optimization}", "-Wall", "-Wextra", "-Werror",
                                            str(path), "-o", str(exe)], capture_output=True, text=True, timeout=60)
                    self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
                    env = dict(os.environ)
                    env["HWAOI_CORE_STALL_DIAG"] = "0"
                    run = subprocess.run([str(exe)], env=env, capture_output=True, text=True, timeout=15)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    self.assertIn("checks passed", run.stdout)


if __name__ == "__main__":
    unittest.main()
