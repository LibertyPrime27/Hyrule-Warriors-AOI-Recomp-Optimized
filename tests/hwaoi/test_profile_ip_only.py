"""Run the production Windows sampler loop with controlled OS calls, no game.

IP-only must still select the main/render thread and frame window while never
querying or copying the deliberately invalid stack address, or unwinding it.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")

HEAD = r'''
#include <windows.h>
#include <psapi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define render_tid 43u
#define main_tid 42u
#define replay_tid 44u
static unsigned checks, samples, stack_counts, queries, unwinds, suspends, resumes, creates, writes;
static int64_t world_us;
static int expect_ip, fail_context, fail_suspend, fail_create;
static const char *opt_main,*opt_render,*opt_slow,*opt_chains,*opt_ip,*opt_frames="1400-2000";
static unsigned char real_stack[32];
#define CHECK(x) do { checks++; if(!(x)) { fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1); } } while(0)
static const char *cfg_get(const char *name) {
    if(!strcmp(name,"TOTK_PROFILE")) return "test-profile.txt";
    if(!strcmp(name,"TOTK_PROFILE_MAIN")) return opt_main;
    if(!strcmp(name,"TOTK_PROFILE_RENDER")) return opt_render;
    if(!strcmp(name,"TOTK_PROFILE_SLOW")) return opt_slow;
    if(!strcmp(name,"TOTK_PROFILE_CHAINS")) return opt_chains;
    if(!strcmp(name,"TOTK_PROFILE_IP_ONLY")) return opt_ip;
    if(!strcmp(name,"TOTK_PROFILE_FRAMES")) return opt_frames;
    if(!strcmp(name,"TOTK_PROFILE_BUSY")) return NULL;   // busy-thread filter off: every selected thread is sampled
    CHECK(0);return NULL;
}
static void rt_log(const char *format,...) { (void)format; }
static uint64_t kern_now_ns(void) { return 100000; }
static int64_t frame_step_world_us(void) { return world_us; }
'''

STUBS = r'''
static void count_rip(uint64_t rip) { CHECK(rip==0x1800);samples++; }
static void count_stack(const uint64_t *rip,uint32_t depth) { CHECK(rip[0]==0x1800 && depth==1);stack_counts++; }
static uint32_t unwind_copy(CONTEXT *ctx,uintptr_t lo,size_t n,uint64_t *rip) {
    CHECK(!expect_ip);CHECK(lo==(uintptr_t)real_stack && n==sizeof real_stack);
    CHECK(!memcmp(real_stack,stk_buf,sizeof real_stack));
    unwinds++;rip[0]=ctx->Rip;ctx->Rip=0x3000;return 1; // caller outside the image must not reclassify the sample
}
static void refresh_threads(DWORD self) { CHECK(self==99); }
static void slow_dump(void) { CHECK(0); }
static void prof_sampler_write(void) { CHECK(!written);writes++;written=1;stop=1; }
static DWORD m_GetCurrentThreadId(void) { return 99; }
static HANDLE m_GetCurrentThread(void) { return (HANDLE)(uintptr_t)99; }
static BOOL m_SetThreadPriority(HANDLE h,int priority) { CHECK(h==(HANDLE)(uintptr_t)99);(void)priority;return TRUE; }
static HANDLE m_CreateWaitableTimerExW(void *a,void *b,DWORD c,DWORD d) { (void)a;(void)b;(void)c;(void)d;return NULL; }
static BOOL m_SetWaitableTimer(HANDLE a,LARGE_INTEGER *b,int c,void *d,void *e,BOOL f) {
    (void)a;(void)b;(void)c;(void)d;(void)e;(void)f;CHECK(0);return FALSE;
}
static DWORD m_WaitForSingleObject(HANDLE h,DWORD wait) { (void)h;(void)wait;CHECK(0);return 0; }
static void m_Sleep(DWORD ms) { CHECK(ms==1);stop=1; } // one loop iteration
static BOOL m_QueryThreadCycleTime(HANDLE h,uint64_t *cycles) { (void)h;(void)cycles;CHECK(!expect_ip);return FALSE; }
static DWORD m_SuspendThread(HANDLE h) { CHECK(h==(HANDLE)(uintptr_t)42 || h==(HANDLE)(uintptr_t)43);suspends++;return fail_suspend ? (DWORD)-1 : 0; }
static BOOL m_GetThreadContext(HANDLE h,CONTEXT *ctx) {
    (void)h;CHECK(ctx->ContextFlags==(expect_ip ? CONTEXT_CONTROL : (CONTEXT_CONTROL|CONTEXT_INTEGER)));
    ctx->Rip=0x1800;ctx->Rsp=expect_ip ? 1 : (uintptr_t)real_stack;return !fail_context;
}
static SIZE_T m_VirtualQuery(void *address,MEMORY_BASIC_INFORMATION *mb,SIZE_T size) {
    (void)size;CHECK(!expect_ip && address==real_stack);queries++;
    mb->BaseAddress=real_stack;mb->RegionSize=sizeof real_stack;mb->State=MEM_COMMIT;mb->Protect=PAGE_READWRITE;
    return sizeof *mb;
}
static DWORD m_ResumeThread(HANDLE h) { (void)h;resumes++;return 0; }
static HANDLE m_GetCurrentProcess(void) { return (HANDLE)(uintptr_t)5; }
static HMODULE m_GetModuleHandleW(void *name) { CHECK(!name);return (HMODULE)(uintptr_t)0x1000; }
static BOOL m_GetModuleInformation(HANDLE p,HMODULE m,MODULEINFO *info,DWORD size) {
    (void)p;(void)m;(void)size;info->lpBaseOfDll=(void *)(uintptr_t)0x1000;info->SizeOfImage=0x1000;return TRUE;
}
static HANDLE m_CreateThread(void *a,SIZE_T b,LPTHREAD_START_ROUTINE c,void *d,DWORD e,DWORD *f) {
    (void)a;(void)b;(void)c;(void)d;(void)e;(void)f;creates++;return fail_create ? NULL : (HANDLE)(uintptr_t)7;
}
static DWORD m_GetLastError(void) { return 123; }
#define GetCurrentThreadId m_GetCurrentThreadId
#define GetCurrentThread m_GetCurrentThread
#define SetThreadPriority m_SetThreadPriority
#define CreateWaitableTimerExW m_CreateWaitableTimerExW
#define SetWaitableTimer m_SetWaitableTimer
#define WaitForSingleObject m_WaitForSingleObject
#define Sleep m_Sleep
#define QueryThreadCycleTime m_QueryThreadCycleTime
#define SuspendThread m_SuspendThread
#define GetThreadContext m_GetThreadContext
#define VirtualQuery m_VirtualQuery
#define ResumeThread m_ResumeThread
#define GetCurrentProcess m_GetCurrentProcess
#define GetModuleHandleW m_GetModuleHandleW
#define GetModuleInformation m_GetModuleInformation
#define CreateThread m_CreateThread
#define GetLastError m_GetLastError
'''

TAIL = r'''
static void sample_at(uint64_t frame,unsigned want) {
    cur_frame=frame;stop=0;
    uint64_t in_before=threads[0].in_exe+threads[1].in_exe;
    samples=stack_counts=queries=unwinds=suspends=resumes=0;
    CHECK(sampler_main(NULL)==0);CHECK(samples==want);
    if(expect_ip) CHECK(!queries && !unwinds && !stack_counts && !ring_n && !stall_n);
    else CHECK(queries==want && unwinds==want && stack_counts==want);
    CHECK(resumes==(fail_suspend ? 0 : suspends));
    CHECK(threads[0].in_exe+threads[1].in_exe-in_before==want);
}
static void start(void) {
    frame_lo=frame_hi=0;world_lo=world_hi=0;slow_ns=0;chains=0;ring_n=stall_n=0;
    CHECK(prof_sampler_start()==1);CHECK(frame_lo==1400 && frame_hi==2000);
    CHECK(only_main==!!opt_main && only_render==!!opt_render);
    memset(threads,0,sizeof threads);n_threads=2;
    threads[0].tid=42;threads[0].h=(HANDLE)(uintptr_t)42;
    threads[1].tid=43;threads[1].h=(HANDLE)(uintptr_t)43;
    if(expect_ip) CHECK(!chains && !slow_ns);
    else CHECK(chains);
}
int main(int argc,char **argv) {
    CHECK(argc==2);(void)argv;
    memset(real_stack,0xa5,sizeof real_stack);
    expect_ip=1;opt_main="1";opt_ip="1";opt_slow="50";opt_chains="1";
    start();sample_at(1399,0);sample_at(1400,1);sample_at(1999,1);sample_at(2000,0);
    fail_context=1;sample_at(1500,0);CHECK(suspends==1 && resumes==1);fail_context=0;
    fail_suspend=1;sample_at(1500,0);CHECK(suspends==1 && !resumes);fail_suspend=0;
    opt_main=NULL;opt_render="1";start();sample_at(1500,1);
    opt_render=NULL;start();sample_at(1500,2);
    // With the new opt-in absent or explicitly zero, MAIN/RENDER retains its
    // historical stack behavior even when PROFILE_CHAINS is explicitly zero.
    expect_ip=0;opt_main="1";opt_ip=NULL;opt_slow=NULL;opt_chains="0";
    start();sample_at(1500,1);
    opt_ip="0";start();sample_at(1500,1);
    opt_main=NULL;opt_render="1";start();sample_at(1500,1);
    opt_render=NULL;opt_chains="1";start();sample_at(1500,2);
    // Finite profiles become available without closing a running game. Flush
    // at the exclusive upper bound, exactly once; unbounded runs keep sampling.
    written=0;slow_ns=0;frame_lo=1400;frame_hi=2000;world_hi=0;
    prof_sampler_frame(1399,100000000,0);prof_sampler_frame(1999,100000000,0);CHECK(!writes);
    prof_sampler_frame(2000,100000000,0);CHECK(writes==1&&written&&stop);
    prof_sampler_frame(2001,100000000,0);CHECK(writes==1);
    written=0;frame_hi=0;world_lo=100;world_hi=200;world_us=199;
    prof_sampler_frame(3000,100000000,0);CHECK(writes==1);
    world_us=200;prof_sampler_frame(3001,100000000,0);CHECK(writes==2&&written);
    written=0;world_hi=0;prof_sampler_frame(UINT64_MAX,100000000,0);CHECK(writes==2);
    out_path=NULL;frame_hi=10;prof_sampler_frame(20,100000000,0);CHECK(writes==2);
    fail_create=1;CHECK(prof_sampler_start()==0 && out_path==NULL);
    CHECK(creates==8);
    printf("%u checks passed\n",checks);return 0;
}
'''


def fixture_source():
    source = (ROOT / "runtime/prof_win.c").read_text(encoding="utf-8")
    declarations = source[source.index("#define PROF_SLOTS"):source.index("static void count_rip")]
    loop = source[source.index("static int in_window("):source.index("static int cmp_desc(")]
    start = source[source.index("int prof_sampler_start("):source.index("// perf_present, every present")]
    frame = source[source.index("void prof_sampler_frame("):]
    return HEAD + declarations + STUBS + loop + start + frame + TAIL


@unittest.skipUnless(CC.is_file(), "native fixture requires F:/llvm-mingw")
class ProfileIpOnlyTests(unittest.TestCase):
    def test_real_sampler_ip_only_and_legacy_modes(self):
        env = dict(os.environ)
        env["PATH"] = str(CC.parent) + os.pathsep + env.get("PATH", "")
        with tempfile.TemporaryDirectory(prefix="hwaoi-profile-ip-") as tmp:
            for opt in ("O0", "O2"):
                with self.subTest(optimization=opt):
                    src = Path(tmp) / f"fixture-{opt}.c"
                    src.write_text(fixture_source(), encoding="utf-8")
                    exe = src.with_suffix(".exe")
                    built = subprocess.run([str(CC), f"-{opt}", "-std=c11", str(src), "-o", str(exe)],
                                           capture_output=True, text=True, env=env)
                    self.assertEqual(built.returncode, 0, built.stderr)
                    result = subprocess.run([str(exe), "all"], capture_output=True,
                                            text=True, env=env, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
