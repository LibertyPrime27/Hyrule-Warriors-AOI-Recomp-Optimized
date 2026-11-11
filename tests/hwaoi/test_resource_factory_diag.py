"""Factory failure observation preserves original calls, CPU results and host scopes."""
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
class ResourceFactoryDiagnosticTests(unittest.TestCase):
    def test_original_result_and_stack_descriptor_snapshot(self):
        helper = function((ROOT / "runtime/hwaoi_pool_diag.h").read_text(encoding="utf-8"),
                          "static void hwaoi_resource_factory_diag(")
        source = r'''
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <assert.h>
typedef struct {uint64_t x[32],pc;} Cpu;
typedef void (*GuestFn)(Cpu*);
static uint64_t data[4][12];
static struct {uint64_t executor_lr;} hwaoi_pool_diag;
static unsigned depth,calls,reads,logs;static uint64_t result=1,snapshot;
static void original(Cpu *c){assert(!depth);calls++;c->x[0]=result;c->x[1]=0xaabbcc;
 memset(data,0,sizeof data);}
static GuestFn rt_dispatch_lookup(uint64_t pc){assert(pc==0x40000);return original;}
static void *hwaoi_host_section_enter(Cpu *c){(void)c;depth++;return (void*)1;}
static void hwaoi_host_section_leave(void *p){assert(p==(void*)1&&depth==1);depth--;}
static int plat_mem_read(uint64_t p,void *out,size_t n){
 assert(depth==1);reads++;
 for(unsigned i=0;i<4;i++)if(p==(uintptr_t)data[i]&&n==sizeof data[i]){memcpy(out,data[i],n);return 1;}
 return 0;
}
static unsigned plat_thread_id(void){return 12;}
static void rt_fatal(const char *fmt,...){(void)fmt;abort();}
static void rt_log(const char *fmt,...){assert(depth==1);logs++;va_list ap;va_start(ap,fmt);
 if(!strcmp(fmt,"resource-factory: arg%u+%#x %#llx %#llx %#llx %#llx")){
  unsigned i=va_arg(ap,unsigned),at=va_arg(ap,unsigned);uint64_t value=va_arg(ap,unsigned long long);
  if(!i&&!at)snapshot=value;
 }va_end(ap);}
''' + helper + r'''
int main(void){
 Cpu c={.pc=0x40000};for(unsigned i=0;i<4;i++)c.x[i]=(uintptr_t)data[i];
 hwaoi_resource_factory_diag(&c);assert(calls==1&&!depth&&reads==4&&!logs&&c.x[0]==1&&c.x[1]==0xaabbcc);
 for(unsigned i=0;i<4;i++)c.x[i]=(uintptr_t)data[i];data[0][0]=0x1122334455667788ull;
 result=0;hwaoi_resource_factory_diag(&c);assert(calls==2&&!depth&&logs==13&&snapshot==0x1122334455667788ull);
 assert(c.x[0]==0&&c.x[1]==0xaabbcc&&!data[0][0]); // observed bytes precede the original's writes
 reads=0;logs=0;c.x[0]=0;c.x[1]=UINT64_MAX-15;c.x[2]=0x10;c.x[3]=0;
 hwaoi_resource_factory_diag(&c);assert(reads==1&&logs==1&&calls==3&&!depth);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "factory.c"; path.write_text(source)
            for opt in ("-O0", "-O2"):
                exe = path.with_suffix(".exe")
                subprocess.run([os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe", opt, "-Wall", "-Wextra", "-Werror",
                                str(path), "-o", str(exe)], check=True, capture_output=True)
                subprocess.run([str(exe)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
