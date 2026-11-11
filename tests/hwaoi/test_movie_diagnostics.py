"""Original native fixtures for bounded, behavior-preserving media diagnostics."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")


def function(text, signature):
    begin = text.index(signature)
    end = text.index("{", begin) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[begin:end]


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Existing Windows Clang required")
class MovieDiagnosticTests(unittest.TestCase):
    def test_romfs_results_and_demux_observers_preserve_behavior(self):
        rom = (ROOT / "runtime/romfs.c").read_text(encoding="utf-8")
        movie = (ROOT / "runtime/ov_movie.c").read_text(encoding="utf-8")
        source = r'''
#include "host.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"line %d: %s\n",__LINE__,#x); abort(); } } while(0)
static int enabled, logs, original_calls;
static char last[1024];
uint32_t rt_trace;
const char *cfg_get(const char *key) { CHECK(!strcmp(key,"TOTK_FS_DIAG")); return enabled ? "1" : "0"; }
void rt_log(const char *fmt, ...) {
    va_list a; va_start(a,fmt); vsnprintf(last,sizeof last,fmt,a); va_end(a); logs++;
}
static Cpu before, expected;
uint64_t rt_original(Cpu *c) {
    CHECK(!memcmp(c,&before,sizeof *c)); original_calls++;
    *c=expected; return c->x[0];
}
'''
        source += function(rom, "static uint32_t romfs_open_result(") + "\n"
        source += function(movie, "static void movie_extract_observe(") + "\n"
        source += r'''
int main(int argc,char **argv) {
    CHECK(argc==2); enabled=!strcmp(argv[1],"on");
    uint64_t out=UINT64_C(0x100000009);
    CHECK(romfs_open_result("rom:/original",1,&out,0)==0 && out==UINT64_C(0x100000009));
    CHECK(logs==enabled);
    if(enabled) CHECK(strstr(last,"mode=0x1 result=0 handle=0x100000009 successes=1 failures=0"));
    for(int i=1;i<32;i++) CHECK(romfs_open_result("rom:/original",1,&out,0)==0);
    CHECK(logs==(enabled ? 32 : 0));
    for(int i=0;i<100;i++) CHECK(romfs_open_result(NULL,3,(uint64_t*)1,0x6402)==0x6402);
    CHECK(logs==(enabled ? 64 : 0));
    if(enabled) CHECK(strstr(last,"mode=0x3 result=0x6402 handle=0 ") && strstr(last,"path=(null)"));
    for(int i=132;i<16384;i++) CHECK(romfs_open_result("rom:/original",1,&out,0)==0);
    CHECK(logs==(enabled ? 65 : 0));
    if(enabled) CHECK(strstr(last,"seq=16384") && strstr(last,"successes=16284 failures=100"));
    CHECK(out==UINT64_C(0x100000009));
    logs=0; rt_trace=enabled ? RT_TRACE_CODEC : 0;
    memset(&before,0xa5,sizeof before); expected=before;
    expected.x[0]=UINT64_C(0x1234567887654321); expected.x[2]=0x4321;
    expected.pc=0x1234; expected.v[0]=0x5678;
    for(unsigned which=0;which<7;which++) {
        int base_logs=logs;
        for(int i=0;i<16384;i++) {
            Cpu c=before; movie_extract_observe(&c,which); CHECK(!memcmp(&c,&expected,sizeof c));
        }
        CHECK(logs-base_logs==(enabled ? 34 : 0));
        if(enabled) CHECK(strstr(last,"call=16384") && strstr(last,"x0=0x1234567887654321"));
    }
    CHECK(original_calls==7*16384);
    puts("movie diagnostic fixture passed"); return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="movie-diag-", dir=ROOT / "work/build") as temp:
            path = Path(temp) / "fixture.c"
            path.write_text(source, encoding="utf-8")
            for optimization in ("0", "2"):
                exe = Path(temp) / f"fixture-{optimization}.exe"
                result = subprocess.run([str(CC), "-std=gnu11", f"-O{optimization}", "-march=x86-64-v3",
                                         "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "runtime"),
                                         str(path), "-o", str(exe)], capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for mode in ("on", "off"):
                    with self.subTest(optimization=optimization, mode=mode):
                        result = subprocess.run([str(exe), mode], capture_output=True, text=True, timeout=20)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn("fixture passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
