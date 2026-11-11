"""Actual retained-memory accounting with synthetic host allocations only.

The fixture runs the production owner snapshots and program ledger, including
allocation failure and concurrent sampling. It never runs game code or a GPU.
Compiling/running this fixture is deliberately deferred while builds are paused.
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
sys.path.insert(0, str(ROOT / "tools"))
from hwaoi_probe import summarize_runtime_diagnostics


def source():
    cmd = (ROOT / "runtime/nvn_api_cmd.c").read_text(encoding="utf-8")
    obj = (ROOT / "runtime/nvn_api_obj.c").read_text(encoding="utf-8")
    copies = cmd[cmd.index("// ---------- texture pins"):cmd.index("// ---------- CommandBuffer: object")]
    programs = obj[obj.index("// The diagnostic ledger owns no shader bytes"):obj.index("// NVNboolean nvnProgramInitialize(")]
    snapshot = function(cmd, "void nvn_runtime_memory_stats(")
    enabled = function(cmd, "int nvn_memory_diagnostics(")
    return r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <windows.h>
#include "nvn_api.h"
#include "nvn_hmap.h"
#include "platform/platform.h"
#define CHECK(x) do {if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
#undef NVN_LOG
#define NVN_LOG(...) ((void)0)
#define COPY_MAGIC UINT64_C(0x59504f43444d43)
#define COPY_PAGE_SHIFT 16
#define CTL_PER_RECORDING 64
static const char *setting;
static unsigned config_reads, ledger_warnings, fail_index;
static uint64_t replayed;
const char *cfg_get(const char *key) {
 CHECK(!strcmp(key,"TOTK_RUNTIME_MEMORY_DIAG"));
 __atomic_fetch_add(&config_reads,1,__ATOMIC_RELAXED);return setting;
}
void rt_log(const char *fmt,...) {CHECK(strstr(fmt,"program allocation ledger disabled"));ledger_warnings++;}
void rt_fatal(const char *fmt,...) {va_list ap;va_start(ap,fmt);vfprintf(stderr,fmt,ap);va_end(ap);exit(73);}
void plat_mutex_lock(PlatMutex *p) {AcquireSRWLockExclusive((PSRWLOCK)p);}
void plat_mutex_unlock(PlatMutex *p) {ReleaseSRWLockExclusive((PSRWLOCK)p);}
void plat_mutex_lock_shared(PlatMutex *p) {AcquireSRWLockShared((PSRWLOCK)p);}
void plat_mutex_unlock_shared(PlatMutex *p) {ReleaseSRWLockShared((PSRWLOCK)p);}
uint64_t gfx_replayed_submits(void) {return replayed;}
''' + enabled + copies + r'''
// Only diagnostic index allocation can be failed. The ledger must not free or
// mutate the shader/control allocation that belongs to the caller.
static void *index_malloc(size_t n) {if(fail_index){fail_index--;return NULL;}return malloc(n);}
static void *index_calloc(size_t n,size_t s) {if(fail_index){fail_index--;return NULL;}return calloc(n,s);}
#define malloc index_malloc
#define calloc index_calloc
''' + programs + r'''
#undef malloc
#undef calloc
''' + snapshot + r'''
static NvnMemoryStats stats(void) {NvnMemoryStats s;memset(&s,0xcc,sizeof s);nvn_runtime_memory_stats(&s);return s;}
static void copies_and_pins(void) {
 op_hdr records[2]={{OP_BEGIN,8},{OP_END,8}};
 uint8_t *a=flatten_recording((const uint8_t *)records,sizeof records);
 uint8_t *b=flatten_recording((const uint8_t *)records,sizeof records);
 copy_add(0x10000,0x10001,a);copy_add(0x20000,0x20001,b);
 CHECK(nvn_handle_live((uintptr_t)a) && nvn_handle_bytes((uintptr_t)a)==sizeof records);
 NvnMemoryStats s=stats();CHECK(s.copies==2 && s.copies_made==2);
 CHECK(s.copy_bytes==2*(sizeof(copy_prefix)+sizeof records) && s.copy_index_bytes>0);
 uint64_t bytes=s.copy_bytes;((copy_prefix *)a-1)->used_at=10;
 release_copies(0x10000,0x10001);CHECK(!nvn_handle_live((uintptr_t)a));
 s=stats();CHECK(s.copies==1 && s.zombies==1 && s.copy_bytes+s.zombie_bytes==bytes);
 // Reading counters does not sweep pending work, even when it has completed.
 replayed=10;s=stats();CHECK(s.zombies==1);
 plat_mutex_lock(&copy_lock);zombie_sweep(9);plat_mutex_unlock(&copy_lock);CHECK(stats().zombies==1);
 plat_mutex_lock(&copy_lock);zombie_sweep(10);plat_mutex_unlock(&copy_lock);CHECK(!stats().zombies);
 release_copies(0x20000,0x20001);s=stats();CHECK(!s.copies && !s.copy_bytes && !s.zombie_bytes);
 CHECK(s.copies_made==2);nvn_runtime_memory_stats(NULL);
 tex_t t={.serial=11};CHECK(!tex_pin(NULL));const tex_t *p=tex_pin((const NVNtexture *)&t);
 CHECK(p && p!=&t && tex_pin((const NVNtexture *)&t)==p);
 t.serial=12;t.d.width=99;const tex_t *q=tex_pin((const NVNtexture *)&t);
 CHECK(q!=p && p->serial==11 && !p->d.width && q->d.width==99);
 s=stats();CHECK(s.texture_pins==2 && s.texture_pin_bytes==2*sizeof(tex_t) && s.texture_pin_index_bytes>0);
}
static void controls(void) {
 unsigned char *a=malloc(64),*b=malloc(128);CHECK(a && b);memset(a,0xa5,64);memset(b,0x5a,128);
 program_memory_allocated(a,64);program_memory_allocated(b,128);NvnMemoryStats s=stats();
 CHECK(s.program_controls==2 && s.program_control_bytes==192 && s.program_controls_made==2);
 CHECK(s.program_control_bytes_made==192 && s.program_control_index_bytes>0 && !s.program_ledger_disabled);
 program_memory_released(a);s=stats();CHECK(s.program_controls==1 && s.program_control_bytes==128);
 for(unsigned i=0;i<64;i++)CHECK(a[i]==0xa5);free(a);
 // Losing a guest object's pointer does not remove its still-allocated bytes
 // from the independent host ledger. Its subsequent actual release does.
 void *saved=b;b=NULL;s=stats();CHECK(s.program_controls==1 && s.program_control_bytes==128);
 program_memory_allocated(saved,128);s=stats();CHECK(s.program_controls==1 && s.program_control_bytes==128);
 CHECK(s.program_duplicate_allocations==1 && s.program_controls_made==3);
 program_memory_released(saved);free(saved);program_memory_released((const void *)(uintptr_t)1);
 s=stats();CHECK(!s.program_controls && !s.program_control_bytes && s.program_unmatched_frees==1);
 CHECK(s.program_controls_made==3 && s.program_control_bytes_made==320);
 program_memory_allocated(NULL,0);program_memory_released(NULL);CHECK(stats().program_unmatched_frees==1);
}
static DWORD WINAPI produce(void *unused) {
 (void)unused;for(unsigned i=0;i<2000;i++){
  unsigned char *p=malloc(64);CHECK(p);memset(p,0xa7,64);
  program_memory_allocated(p,64);program_memory_released(p);
  CHECK(p[0]==0xa7 && p[63]==0xa7);free(p);
 }return 0;
}
static DWORD WINAPI sample(void *unused) {
 (void)unused;for(unsigned i=0;i<4000;i++){
  NvnMemoryStats s=stats();CHECK(s.program_control_bytes==64*s.program_controls);
  CHECK(!s.program_ledger_disabled && !s.program_unmatched_frees && !s.program_duplicate_allocations);
 }return 0;
}
int main(int argc,char **argv) {
 CHECK(argc==2);const char *mode=argv[1];
 setting=!strcmp(mode,"unset")?NULL:(!strcmp(mode,"0")?"0":(!strcmp(mode,"2")?"2":"1"));
 int on=setting && !strcmp(setting,"1");CHECK(nvn_memory_diagnostics()==on && config_reads==1);
 // Restart-only enablement: later environment/config changes do not create a
 // partial ledger or change allocation/free tracking half way through a run.
 setting=on?"0":"1";CHECK(nvn_memory_diagnostics()==on && config_reads==1);
 if(!on){program_memory_allocated((void *)(uintptr_t)8,16);program_memory_released((void *)(uintptr_t)8);
  NvnMemoryStats s=stats();CHECK(!s.program_controls && !s.program_controls_made && !s.program_control_index_bytes);
 }else if(!strcmp(mode,"oom")){
  unsigned char *p=malloc(32);CHECK(p);memset(p,0x6a,32);fail_index=1;program_memory_allocated(p,32);
  NvnMemoryStats s=stats();CHECK(s.program_ledger_disabled && s.program_controls_made==1 && !s.program_controls);
  CHECK(ledger_warnings==1 && p[0]==0x6a && p[31]==0x6a);
  program_memory_allocated(p,32);CHECK(ledger_warnings==1);program_memory_released(p);free(p);
 }else if(!strcmp(mode,"limit")){
  for(unsigned i=0;i<PROGRAM_MEMORY_MAX_CAP*3/4+1;i++)program_memory_allocated((void *)(uintptr_t)(0x10000+16*i),4);
  NvnMemoryStats s=stats();CHECK(s.program_ledger_disabled && s.program_controls==PROGRAM_MEMORY_MAX_CAP*3/4);
  CHECK(s.program_controls_made==PROGRAM_MEMORY_MAX_CAP*3/4+1 && ledger_warnings==1);
  CHECK(s.program_control_index_bytes<=(UINT64_C(1)<<20));
 }else if(!strcmp(mode,"race")){
  HANDLE threads[3]={CreateThread(NULL,0,produce,NULL,0,NULL),CreateThread(NULL,0,produce,NULL,0,NULL),
                     CreateThread(NULL,0,sample,NULL,0,NULL)};
  CHECK(threads[0] && threads[1] && threads[2]);CHECK(WaitForMultipleObjects(3,threads,TRUE,30000)==WAIT_OBJECT_0);
  for(unsigned i=0;i<3;i++)CloseHandle(threads[i]);NvnMemoryStats s=stats();
  CHECK(!s.program_controls && !s.program_control_bytes && s.program_controls_made==4000);
 }else controls();
 copies_and_pins();CHECK(config_reads==1);puts("retained-memory diagnostic checks passed");return 0;
}
'''


class RuntimeMemorySummaryTests(unittest.TestCase):
    def test_owner_growth_and_rejection_are_bounded_evidence(self):
        log = "\n".join((
            "[host] runtime-memory: frame=600 copy_bytes=1000 zombie_bytes=400 texture_pin_bytes=128 program_control_bytes=900 program_ledger_partial=0",
            "[host] runtime-memory: frame=1200 copy_bytes=2200 zombie_bytes=100 texture_pin_bytes=896 program_control_bytes=2100 program_ledger_partial=0",
            "[host] vk: RGBA8 comparison rejected: reason=image-extent addr=0x123 extent=32x32x1",
            "FATAL: vk: unsupported comparison texture"))
        result = summarize_runtime_diagnostics(log)
        self.assertEqual(result["retained_owner_sample_count"], 2)
        self.assertEqual(result["retained_owner_first"]["frame"], 600)
        self.assertEqual(result["retained_owner_last"]["frame"], 1200)
        self.assertEqual(result["retained_owner_growth_bytes_per_frame"],
                         {"copy_bytes": 2, "zombie_bytes": -.5, "texture_pin_bytes": 1.28, "program_control_bytes": 2})
        self.assertIn("reason=image-extent", result["last_rgba8_comparison_rejection"])
        self.assertEqual(result["last_fatal"], "vk: unsupported comparison texture")

    def test_missing_partial_and_restarted_samples_do_not_invent_growth(self):
        empty = summarize_runtime_diagnostics("[host] another message\n")
        self.assertEqual(empty["retained_owner_sample_count"], 0)
        self.assertIsNone(empty["retained_owner_last"])
        self.assertFalse(empty["retained_owner_growth_bytes_per_frame"])
        log = ("[host] runtime-memory: frame=600 copy_bytes=0 program_control_bytes=1 program_ledger_partial=0\n"
               "[host] runtime-memory: frame=1200 copy_bytes=600 program_control_bytes=1200 program_ledger_partial=1\n")
        self.assertEqual(summarize_runtime_diagnostics(log)["retained_owner_growth_bytes_per_frame"], {"copy_bytes": 1})
        log += "[host] runtime-memory: frame=600 copy_bytes=0\n"
        self.assertFalse(summarize_runtime_diagnostics(log)["retained_owner_growth_bytes_per_frame"])


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Windows Clang required")
class RuntimeMemoryDiagnosticTests(unittest.TestCase):
    def test_actual_owner_counts_and_bounded_ledger(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-runtime-memory-") as folder:
            path = Path(folder) / "fixture.c"
            path.write_text(source(), encoding="utf-8")
            for opt in ("-O0", "-O2"):
                executable = Path(folder) / "fixture.exe"
                compiled = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                    "-Wno-missing-field-initializers", "-I", str(ROOT / "runtime"), str(path), "-o", str(executable)],
                    capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
                for mode in ("1", "0", "unset", "2", "oom", "limit", "race"):
                    with self.subTest(opt=opt, mode=mode):
                        run = subprocess.run([str(executable), mode], capture_output=True, text=True, timeout=45)
                        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
