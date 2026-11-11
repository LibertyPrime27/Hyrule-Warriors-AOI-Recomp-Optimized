"""Actual timing roles/snapshots with independent native host threads only.

The fixture extracts production functions. It never loads a ROM, Vulkan, or the
running game and cannot change the user's paused scene.
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


def source():
    production = (ROOT / "runtime/perf.c").read_text(encoding="utf-8")
    functions = "\n".join(function(production, name) for name in (
        "unsigned perf_replay_scope_begin(", "void perf_replay_scope_end(",
        "static void perf_times_take(", "void perf_end("))
    return r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
enum {PERF_PACE,PERF_FENCE,PERF_DRAIN,PERF_PIPE,PERF_XLATE,PERF_TEX,PERF_ASTC,PERF_N};
static uint64_t wait_ns[PERF_N],pending_frame_ns[PERF_N];
static _Thread_local unsigned replay_scope;
static _Thread_local uint64_t simulated_now=100;
static volatile uint32_t prof_render_tid,prof_replay_tid;
static uint32_t plat_thread_id(void) {return GetCurrentThreadId();}
static uint64_t kern_now_ns(void) {return simulated_now;}
#define CHECK(x) do {if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
''' + functions + r'''
static DWORD WINAPI unscoped_child(void *unused) {
    (void)unused;
    CHECK(!replay_scope);
    simulated_now=1000;
    perf_end(PERF_PIPE,1); // must not inherit the creating thread's scope
    return 0;
}
static void join(HANDLE thread) {
    CHECK(thread && WaitForSingleObject(thread,10000)==WAIT_OBJECT_0);
    DWORD code;CHECK(GetExitCodeThread(thread,&code) && code==0);CloseHandle(thread);
}
static DWORD WINAPI role_checks(void *unused) {
    (void)unused;
    CHECK(!replay_scope && GetCurrentThreadId()!=prof_render_tid && !prof_replay_tid);
    simulated_now=101;
    perf_end(PERF_PIPE,1); // unrelated worker is not a replay worker
    unsigned outer=perf_replay_scope_begin();CHECK(!outer && replay_scope);
    simulated_now=3;perf_end(PERF_PIPE,1); // 2 ns
    unsigned nested=perf_replay_scope_begin();CHECK(nested && replay_scope);
    simulated_now=4;perf_end(PERF_PIPE,1); // 3 ns
    join(CreateThread(NULL,0,unscoped_child,NULL,0,NULL));
    perf_replay_scope_end(nested);CHECK(replay_scope);
    simulated_now=6;perf_end(PERF_PIPE,1); // 5 ns
    perf_end(PERF_PIPE,0); // disabled sample
    perf_replay_scope_end(outer);CHECK(!replay_scope);
    simulated_now=1001;perf_end(PERF_PIPE,1); // restored role must exclude it
    CHECK(!prof_replay_tid); // scoped submissions never overwrite persistent TIDs
    return 0;
}
static DWORD WINAPI async_checks(void *unused) {
    (void)unused;
    prof_replay_tid=GetCurrentThreadId();
    simulated_now=12;perf_end(PERF_XLATE,1); // explicit async role remains accepted
    unsigned outer=perf_replay_scope_begin();
    unsigned nested=perf_replay_scope_begin();
    perf_replay_scope_end(nested);perf_replay_scope_end(outer);
    CHECK(!replay_scope && prof_replay_tid==GetCurrentThreadId());
    simulated_now=14;perf_end(PERF_XLATE,1);
    return 0;
}
#define ITERATIONS 100000
typedef struct {unsigned kind;HANDLE done;} Work;
static DWORD WINAPI producer(void *raw) {
    Work *work=raw;
    CHECK(!replay_scope);
    unsigned previous=perf_replay_scope_begin();
    simulated_now=8;
    for(unsigned i=0;i<ITERATIONS;i++) perf_end((work->kind+i)%PERF_N,1);
    perf_replay_scope_end(previous);CHECK(!replay_scope);
    perf_end(PERF_PIPE,1); // excluded after restoring the role
    SetEvent(work->done);
    return 0;
}
int main(void) {
    prof_render_tid=GetCurrentThreadId();
    join(CreateThread(NULL,0,role_checks,NULL,0,NULL));
    CHECK(wait_ns[PERF_PIPE]==10 && pending_frame_ns[PERF_PIPE]==10);
    uint64_t frame[PERF_N],window[PERF_N];
    perf_times_take(frame,pending_frame_ns);perf_times_take(window,wait_ns);
    CHECK(frame[PERF_PIPE]==10 && window[PERF_PIPE]==10);
    simulated_now=18;perf_end(PERF_TEX,1); // the presentation role still counts
    CHECK(frame[PERF_TEX]==0 && pending_frame_ns[PERF_TEX]==17); // immutable snapshot
    perf_times_take(frame,pending_frame_ns);perf_times_take(window,wait_ns);
    CHECK(frame[PERF_TEX]==17 && window[PERF_TEX]==17);
    join(CreateThread(NULL,0,async_checks,NULL,0,NULL));
    CHECK(wait_ns[PERF_XLATE]==24 && pending_frame_ns[PERF_XLATE]==24);
    prof_replay_tid=0;
    perf_times_take(frame,pending_frame_ns);perf_times_take(window,wait_ns);
    Work work[3];HANDLE threads[3],done[3];
    uint64_t frames[PERF_N]={0},windows[PERF_N]={0},expected[PERF_N]={0};
    for(unsigned t=0;t<3;t++) {
        work[t]=(Work){t,CreateEvent(NULL,TRUE,FALSE,NULL)};CHECK(work[t].done);
        done[t]=work[t].done;
        for(unsigned i=0;i<ITERATIONS;i++) expected[(t+i)%PERF_N]+=7;
        threads[t]=CreateThread(NULL,0,producer,&work[t],0,NULL);CHECK(threads[t]);
    }
    // Atomic exchanges while submissions are being timed must neither drop nor
    // double count a sample. Category/window boundaries may differ, totals may not.
    while(WaitForMultipleObjects(3,done,TRUE,0)==WAIT_TIMEOUT) {
        perf_times_take(frame,pending_frame_ns);perf_times_take(window,wait_ns);
        for(unsigned i=0;i<PERF_N;i++) frames[i]+=frame[i],windows[i]+=window[i];
        SwitchToThread();
    }
    for(unsigned t=0;t<3;t++) join(threads[t]),CloseHandle(done[t]);
    perf_times_take(frame,pending_frame_ns);perf_times_take(window,wait_ns);
    for(unsigned i=0;i<PERF_N;i++) {
        frames[i]+=frame[i];windows[i]+=window[i];
        CHECK(frames[i]==expected[i] && windows[i]==expected[i]);
    }
    CHECK(!replay_scope && !prof_replay_tid && prof_render_tid==GetCurrentThreadId());
    puts("timing scopes: nested roles, isolated workers and 300000 concurrent samples passed");
    return 0;
}
'''


@unittest.skipUnless(CC.is_file() and os.name == "nt", "existing F-drive Windows C compiler required")
class ReplayTimingScope(unittest.TestCase):
    def test_production_roles_and_concurrent_snapshots(self):
        folder = ROOT / "build/test/perf-replay-scope"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temporary:
            temporary = Path(temporary).resolve()
            self.assertTrue(temporary.is_relative_to(folder.resolve()))
            src, exe = temporary / "probe.c", temporary / "probe.exe"
            src.write_text(source(), encoding="utf-8")
            build = subprocess.run([str(CC), "-std=c11", "-O2", "-Wall", "-Wextra", str(src), "-o", str(exe)],
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("300000 concurrent samples passed", run.stdout)


if __name__ == "__main__":
    unittest.main()
