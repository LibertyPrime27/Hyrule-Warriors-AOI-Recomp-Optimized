"""Deliberate guest aborts retain bounded diagnostics without unsafe stack reads."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function


@unittest.skipUnless(os.name == "nt", "Windows native fixture")
class GuestAbortDiagnosticTests(unittest.TestCase):
    def test_valid_corrupt_and_unrelated_guest_frames(self):
        helper = function((ROOT / "runtime/main.c").read_text(encoding="utf-8"),
                          "static void guest_abort_diagnostic(")
        source = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <assert.h>
#define RT_TARGET_HWAOI 1
typedef struct {uint64_t x[32];} Cpu;
typedef struct {const char *name;uint64_t base;} RtLoaded;
static RtLoaded module={"main",0x7000000000ull};
static unsigned logs,reads,messages;static int enabled=1;
static uint64_t stack[6]={0x1020,0x7000040000,0,0,0,0x7000050000};
static int plat_mem_read(uint64_t a,void *out,size_t n){
 reads++;
 if(a>=0x1000 && a<=0x1020 && n==16){memcpy(out,(char*)stack+a-0x1000,n);return 1;}
 if(a==module.base+0x2897fb8 && n==1024){memset(out,0,n);memcpy(out,"synthetic resource assertion",28);messages++;return 1;}
 return 0;
}
static void rt_log(const char *fmt,...){(void)fmt;logs++;}
static void rt_logf(const char *fmt,...){(void)fmt;}
static unsigned plat_thread_id(void){return 17;}
static const char *ldr_describe(uint64_t a,char *b,int n){snprintf(b,n,"%llx",(unsigned long long)a);return b;}
static int rt_patch_set(const char *name){assert(!strcmp(name,"hwaoi_display"));return enabled?0:-1;}
static const RtLoaded *ldr_module(int i){assert(i==0);return &module;}
''' + helper + r'''
int main(void){
 Cpu cpu={0};cpu.x[29]=0x1000;
 guest_abort_diagnostic(&cpu,module.base+0x11334);assert(logs==3&&reads==3&&messages==1);
 logs=reads=messages=0;
 guest_abort_diagnostic(&cpu,module.base+0x11338);assert(logs==2&&reads==2&&messages==0);
 logs=reads=0;stack[0]=0x1000; // a cyclic/mutating guest chain stops after one frame
 guest_abort_diagnostic(&cpu,0);assert(logs==1&&reads==1);
 logs=reads=0;cpu.x[29]=0x1001;guest_abort_diagnostic(&cpu,0);assert(!logs&&!reads);
 cpu.x[29]=UINT64_MAX-15;guest_abort_diagnostic(&cpu,0);assert(!logs&&!reads);
 guest_abort_diagnostic(NULL,module.base+0x11334);assert(!logs&&!reads);
 enabled=0;cpu.x[29]=0;guest_abort_diagnostic(&cpu,module.base+0x11334);assert(!messages);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "abort.c"; path.write_text(source)
            for opt in ("-O0", "-O2"):
                exe = path.with_suffix(".exe")
                subprocess.run([os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe", opt, "-Wall", "-Wextra", "-Werror",
                                str(path), "-o", str(exe)], check=True, capture_output=True)
                subprocess.run([str(exe)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
