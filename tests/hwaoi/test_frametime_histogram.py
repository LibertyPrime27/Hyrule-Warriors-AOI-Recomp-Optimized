"""Synthetic frame intervals exercise the real perf.c summary, without a game.

The reported low cutoff is the fastest frame among the slowest ceil(N/100)
frames, not their mean. Bucket upper bounds produce a conservative FPS bound.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")

HEAD = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include "frametime_hist.h"
enum { PERF_XLATE, PERF_PIPE, PERF_TEX, PERF_ASTC, PERF_FENCE, PERF_DRAIN, PERF_PACE, PERF_N };
static uint64_t frame_ns[PERF_N], clock_ns;
static volatile int64_t frame_replay_ns;
static char log_text[8192];
static size_t log_used;
static unsigned checks;
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr,"line %d: %s\n",__LINE__,#x); exit(1); } } while(0)
static uint64_t kern_now_ns(void) { return clock_ns; }
static int64_t plat_xchg64(volatile int64_t *p,int64_t value) { int64_t old=*p;*p=value;return old; }
static void rt_log(const char *format,...) {
    va_list ap;va_start(ap,format);
    int n=vsnprintf(log_text+log_used,sizeof log_text-log_used,format,ap);
    va_end(ap);CHECK(n>=0 && (size_t)n<sizeof log_text-log_used);
    log_used+=(size_t)n;
    CHECK(log_used+2<sizeof log_text);log_text[log_used++]='\n';log_text[log_used]=0;
}
'''

TAIL = r'''
static int compare_u64(const void *a,const void *b) {
    uint64_t x=*(const uint64_t *)a,y=*(const uint64_t *)b;
    return (x>y)-(x<y);
}
static void interval_bound(uint64_t ns) {
    unsigned b=ft_hist_bin(ns);CHECK(b<FT_BINS);
    uint64_t upper=ft_hist_upper_ns(b);CHECK(upper>=ns);
    if(ns<256000000) CHECK(upper-ns<=250000);
    else CHECK(upper-ns<=ns/32+250000);
    if(ns<UINT64_MAX) CHECK(ft_hist_bin(ns+1)>=b);
}
static void histogram_tests(void) {
    CHECK(ft_hist_bin(0)==0);
    CHECK(ft_hist_upper_ns(ft_hist_bin(16666667))==16750000);
    CHECK(ft_hist_upper_ns(ft_hist_bin(255999999))==256000000);
    CHECK(ft_hist_upper_ns(ft_hist_bin(256000000))==264000000);
    CHECK(ft_hist_upper_ns(ft_hist_bin(1300000000))==1312000000);
    CHECK(ft_hist_upper_ns(ft_hist_bin(2212900000))==2240000000);
    CHECK(ft_hist_upper_ns(ft_hist_bin(UINT64_MAX))==UINT64_MAX);
    CHECK(ft_hist_upper_ns(FT_BINS)==UINT64_MAX);
    CHECK(ft_hist_low_fps(16750000)==59.701);
    CHECK(ft_hist_low_fps(1312000000)==0.762);
    CHECK(ft_hist_low_fps(0)==0);
    CHECK(ft_hist_low_fps(UINT64_MAX)==0);
    // Exercise all representable powers of two, boundaries and deterministic
    // full-width samples. The oracle compares the original interval directly.
    for(unsigned bit=0;bit<64;bit++) {
        uint64_t n=UINT64_C(1)<<bit;
        interval_bound(n-1);interval_bound(n);interval_bound(n+1);
    }
    uint64_t state=UINT64_C(0x0f1e2d3c4b5a6978);
    for(unsigned i=0;i<65536;i++) {
        state=state*UINT64_C(6364136223846793005)+UINT64_C(1442695040888963407);
        interval_bound(state);
    }
    uint32_t hist[FT_BINS]={0};
    CHECK(ft_hist_low_cutoff_ns(hist,0)==0);
    CHECK(ft_hist_low_cutoff_ns(hist,1)==0);
    uint64_t values[1001];
    // Compare the histogram's cutoff against an independently sorted sample
    // for small samples and each ceil(N/100) rank boundary.
    const unsigned sizes[]={1,2,39,99,100,101,199,200,201,1001};
    for(unsigned k=0;k<sizeof sizes/sizeof *sizes;k++) {
        unsigned n=sizes[k];memset(hist,0,sizeof hist);
        for(unsigned i=0;i<n;i++) {
            values[i]=16666667+(uint64_t)((i*37)%113)*16000000;
            hist[ft_hist_bin(values[i])]++;
        }
        qsort(values,n,sizeof *values,compare_u64);
        unsigned slow_count=(n+99)/100;
        uint64_t exact=values[n-slow_count];
        uint64_t upper=ft_hist_low_cutoff_ns(hist,n);
        CHECK(upper>=exact && upper-exact<=exact/32+250000);
        CHECK(ft_hist_low_fps(upper)<=1e9/(double)exact);
    }
}
static void run_summary(unsigned n,uint64_t ns,const char *want) {
    memset(&ft,0,sizeof ft);memset(frame_ns,0,sizeof frame_ns);
    summary_t0=frame_replay_ns=0;log_used=0;log_text[0]=0;
    clock_ns=1;ft_frame(1,clock_ns,0);
    for(unsigned i=0;i<n;i++) {
        clock_ns+=ns;frame_replay_ns=(int64_t)ns-1;
        CHECK(ft_frame(i+2,clock_ns,ns)==ns-1);
    }
    perf_summary_flush();
    if(!strstr(log_text,want)) { fprintf(stderr,"missing %s in:\n%s",want,log_text);exit(1); }
    CHECK(ft.frames==0 && ft.sum==0 && summary_t0==0);
}
static void summary_tests(void) {
    run_summary(3,16666667,"1% low cutoff >= 59.701 fps (bucket upper 16.750 ms)");
    run_summary(39,1300000000,"1% low cutoff >= 0.762 fps (bucket upper 1312.000 ms)");
    run_summary(1,2212900000,"1% low cutoff >= 0.446 fps (bucket upper 2240.000 ms)");
    // A report reset must not leave a stale long-frame bucket in the next run.
    run_summary(1,16666667,"1% low cutoff >= 59.701 fps (bucket upper 16.750 ms)");
    log_used=0;log_text[0]=0;ft_report(clock_ns,"empty");CHECK(log_used==0);
}
int main(int argc,char **argv) {
    CHECK(argc==2);
    if(!strcmp(argv[1],"histogram")) histogram_tests();
    else if(!strcmp(argv[1],"summary")) summary_tests();
    else CHECK(0);
    printf("%u checks passed\n",checks);return 0;
}
'''


def fixture_source():
    source = (ROOT / "runtime/perf.c").read_text(encoding="utf-8")
    begin = source.index("// ---- Frame-time summary")
    end = source.index("// The second line", begin)
    return HEAD + source[begin:end] + TAIL


@unittest.skipUnless(CC.is_file(), "native clang fixture requires F:/llvm-mingw")
class FrameTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hwaoi-frametime-")
        cls.addClassCleanup(cls.tmp.cleanup)
        folder = Path(cls.tmp.name)
        source = fixture_source()
        cls.executables = []
        cls.env = dict(os.environ)
        cls.env["PATH"] = str(CC.parent) + os.pathsep + cls.env.get("PATH", "")
        for opt in ("O0", "O2"):
            src = folder / f"fixture-{opt}.c"
            src.write_text(source, encoding="utf-8")
            exe = src.with_suffix(".exe")
            subprocess.run([str(CC), f"-{opt}", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "runtime"), str(src), "-o", str(exe)],
                           check=True, capture_output=True, text=True, env=cls.env)
            cls.executables.append(exe)
        # A mutation restores the observed 256ms saturation at the production
        # insertion point. The same integration assertion must reject it.
        mutated = source.replace("ft.hist[ft_hist_bin(d)]++;",
                                 "ft.hist[d / 250000 < 1024 ? d / 250000 : 1023]++;")
        if mutated == source:
            raise AssertionError("production histogram insertion not found")
        src = folder / "old-saturation.c"
        src.write_text(mutated, encoding="utf-8")
        cls.mutant = src.with_suffix(".exe")
        subprocess.run([str(CC), "-O2", "-std=c11", "-I", str(ROOT / "runtime"),
                        str(src), "-o", str(cls.mutant)], check=True, capture_output=True,
                       text=True, env=cls.env)

    def test_histogram_bounds_and_cutoff(self):
        for exe in self.executables:
            with self.subTest(exe=exe.name):
                result = subprocess.run([str(exe), "histogram"], capture_output=True,
                                        text=True, env=self.env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("checks passed", result.stdout)

    def test_production_frame_summary(self):
        for exe in self.executables:
            with self.subTest(exe=exe.name):
                result = subprocess.run([str(exe), "summary"], capture_output=True,
                                        text=True, env=self.env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("checks passed", result.stdout)

    def test_old_overflow_mutation_is_rejected(self):
        result = subprocess.run([str(self.mutant), "summary"], capture_output=True,
                                text=True, env=self.env, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing 1% low cutoff >= 0.762 fps", result.stderr)


if __name__ == "__main__":
    unittest.main()
