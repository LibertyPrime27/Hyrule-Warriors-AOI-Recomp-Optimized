"""CPU lifetime checks of actual ProgramSetShaders and its canonical host controls.

The actual prog_t/header, initializer, setter, release, finalizer, discriminator
and metadata packer execute. The allocator tracks and quarantines owned slabs;
the backend and lowerer are bounded spies, never a GPU or native lowerer. A
guard-page compact control detects accidental legacy-tail reads on Windows.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from nvn2_compute_c0_test import function

ROOT = Path(__file__).resolve().parents[1]


def source(control="fixed"):
    obj = (ROOT / "runtime/nvn_api_obj.c").read_text()
    shader = (ROOT / "runtime/gfx_shader.c").read_text()
    release = function(obj, "static void prog_release(")
    setter = function(obj, "static int prog_set_shaders(")
    if control == "borrow":
        setter = setter.replace("next[i].control = saved_ctl;", "next[i].control = ctl;")
    elif control == "early-free":
        old = "    if (o->serial) gfx_backend()->program_release(o);"
        assert release.count(old) == 1
        release = release.replace(old, "    free(o->owned_control);\n    o->owned_control = NULL;\n" + old)
    before = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#else
#include <sys/mman.h>
#include <unistd.h>
#endif
#include "gfx.h"
#include "nvn_geometry_control.h"
static unsigned checks, failures, releases, allocations, frees, fail_alloc, lowers, dumps;
#define CHECK(c,m) do { checks++; if (!(c)) { failures++; fprintf(stderr,"FAIL: %s\n",m); } } while (0)
typedef struct { void *p; size_t n; int live; } Block;
static Block blocks[128];
static unsigned block_count;
static Block *block_of(const void *p) {
    for (unsigned i=0;i<block_count;i++) if(blocks[i].p==p)return &blocks[i];
    return NULL;
}
static void *tracked_malloc(size_t n) {
    if (fail_alloc) { fail_alloc--;return NULL; }
    if(block_count==128)abort();
    void *p=malloc(n);if(!p)abort();
    memset(p,0xcc,n);blocks[block_count++]=(Block){p,n,1};allocations++;return p;
}
static void tracked_free(void *p) {
    if(!p)return;
    Block *b=block_of(p);
    CHECK(b && b->live,"only a live owned control slab is freed once");
    if(!b || !b->live)return;
    b->live=0;memset(p,0xdd,b->n);frees++;
}
// This lifecycle fixture keeps the optional retained-memory diagnostic off.
// Its separate fixture exercises the actual ledger and owner-lock snapshots.
static void program_memory_allocated(const void *p,size_t n) {(void)p;(void)n;}
static void program_memory_released(const void *p) {(void)p;}
static unsigned live_blocks(void) {
    unsigned n=0;for(unsigned i=0;i<block_count;i++)n+=(unsigned)blocks[i].live;return n;
}
typedef struct { void *object; uint64_t result; } TestCall;
#define NVN_API(name) static void name(TestCall *c)
#define NVN_PROG_MAGIC MAGIC('P','R','O','G')
#define arg_p(c,i) ((void)(i),(c)->object)
#define ret_x(c,value) ((c)->result=(value))
#define NVN_LOG(...) ((void)0)
void rt_log(const char *format,...) {(void)format;}
int plat_mem_read(uint64_t address, void *out, size_t size) {
#ifdef _WIN32
    SIZE_T read = 0;
    return ReadProcessMemory(GetCurrentProcess(), (const void *)(uintptr_t)address, out, size, &read) && read == size;
#else
    memcpy(out, (const void *)(uintptr_t)address, size); return 1;
#endif
}
static uint64_t serial_counter;
uint64_t nvn_new_serial(void) {return ++serial_counter;}
void vk_exec_program_registered(const prog_t *p) {(void)p;}   // the back end's early compute translations
void gfx_shader_nvn2_dump(const uint8_t *ctl,const uint8_t *code) {(void)ctl;(void)code;dumps++;}
static const uint8_t *lower_control, *lower_code;
static int lower_success;
int nvn2_lower(const uint8_t *ctl,const uint8_t *code,const uint8_t **out_ctl,const uint8_t **out_code) {
    (void)ctl;(void)code;lowers++;
    if(!lower_success)return 0;
    *out_ctl=lower_control;*out_code=lower_code;return 1;
}
static prog_t expected;
static uint8_t expected_control[6*0x880];
static unsigned expect_release;
static void arm_release(const prog_t *p) {
    expected=*p;expect_release=p->serial!=0;
    if(p->owned_control) {
        Block *b=block_of(p->owned_control);
        if(b && b->live)memcpy(expected_control,p->owned_control,b->n);
    }
}
static void backend_release(const prog_t *p) {
    releases++;
    CHECK(expect_release,"backend release occurs only at the expected successful transition");
    CHECK(p->serial==expected.serial && p->nstage==expected.nstage,"backend sees the old serial and stage count");
    CHECK(p->owned_control==expected.owned_control && !memcmp(p->sd,expected.sd,sizeof p->sd),
          "backend sees the old stage list and owned pointer before replacement");
    if(expected.owned_control) {
        Block *b=block_of(expected.owned_control);
        CHECK(b && b->live,"backend drains while the old owned snapshot is still live");
        CHECK(b && !memcmp(expected.owned_control,expected_control,b->n),"backend sees unmodified old snapshot bytes");
    }
    expect_release=0;
}
static const GfxBackend backend={.program_release=backend_release};
const GfxBackend *gfx_backend(void) {return &backend;}
#define malloc tracked_malloc
#define free tracked_free
'''
    actual = "\n".join((function(shader, "int gfx_shader_is_nvn2("), function(shader, "int gfx_shader_nvn2_sizes("),
        function(obj, "NVN_API(nvnProgramInitialize)"), release,
        function(obj, "NVN_API(nvnProgramFinalize)"), setter))
    after = r'''
#undef malloc
#undef free
static void initialize(prog_t *p) {
    TestCall c={p,0};nvnProgramInitialize(&c);
    CHECK(c.result==1 && p->magic==NVN_PROG_MAGIC && !p->serial && !p->owned_control && !p->nstage,
          "actual initializer clears the complete actual program object");
}
static void finalize(prog_t *p) {
    arm_release(p);TestCall c={p,0};nvnProgramFinalize(&c);
    CHECK(!p->magic && !p->serial && !p->owned_control,"actual finalizer clears identity and owned storage");
    CHECK(!expect_release,"finalizer completed its backend release");
}
static void authored(uint8_t ctl[0x880],uint8_t code[0x90],unsigned location) {
    memset(ctl,0,0x880);memset(code,0,0x90);
    nvn_geometry_control_put(ctl+0x700,0x90);nvn_geometry_control_put(ctl+0x704,0x90);
    nvn_geometry_control_put(ctl+0x714,2);
    nvn_geometry_control_put(code+0x30,1|(3<<5)|(4<<10)|(1u<<24));
    nvn_geometry_control_put(code+0x38,1u<<24);nvn_geometry_control_put(code+0x3c,7u<<24);
    nvn_geometry_control_put(code+0x40,1);
    uint32_t skip[8]={0x0fffffff,UINT32_MAX,UINT32_MAX,UINT32_MAX,UINT32_MAX,UINT32_MAX,UINT32_MAX,UINT32_MAX};
    skip[1]&=~(15u<<(location*4));
    CHECK(nvn_geometry_control_pack_portable(ctl,0x880,code,0x90,skip),"original canonical control authored by actual packer");
}
static NVNshaderData stage(const void *ctl,const void *code) {
    return (NVNshaderData){(NVNbufferAddress)(uintptr_t)code,ctl};
}
static int set(prog_t *p,int n,const NVNshaderData *sd) {
    arm_release(p);int ok=prog_set_shaders(p,n,sd);
    if(ok)CHECK(!expect_release,"successful setter completed the expected backend release");
    else expect_release=0;
    return ok;
}
static void rejected(prog_t *p,int n,const NVNshaderData *sd,const char *message) {
    prog_t old=*p;uint8_t bytes[6*0x880];size_t size=0;
    if(p->owned_control){Block *b=block_of(p->owned_control);if(b && b->live){size=b->n;memcpy(bytes,b->p,size);}}
    unsigned release_before=releases,live_before=live_blocks();uint64_t counter=serial_counter;
    CHECK(!set(p,n,sd),message);
    CHECK(!memcmp(p,&old,sizeof old),"failed preparation preserves every old object byte");
    CHECK(!size || !memcmp(old.owned_control,bytes,size),"failed preparation preserves owned snapshot bytes");
    CHECK(releases==release_before && serial_counter==counter && live_blocks()==live_before,
          "failed preparation neither releases nor assigns a serial nor leaks a slab");
}
static void lifecycle(void) {
    _Alignas(prog_t) uint8_t storage[0xa0+32];memset(storage,0xa5,sizeof storage);
    prog_t *p=(prog_t *)storage;initialize(p);
    CHECK(sizeof(prog_t)<=0xa0,"actual program fits the known 0xa0 allocation stride");
    uint8_t a[0x880],b[0x880],code[0x90],code_b[0x90],legacy[0x880]={0};
    authored(a,code,0);authored(b,code_b,1);
    uint8_t a_original[0x880],b_original[0x880],code_original[0x90];
    memcpy(a_original,a,sizeof a);memcpy(b_original,b,sizeof b);memcpy(code_original,code,sizeof code);
    NVNshaderData sd[2]={stage(legacy,code),stage(a,code)};
    CHECK(set(p,2,sd),"legacy VS plus canonical GS binds successfully");
    CHECK(p->owned_control && p->sd[1].control!=a && p->sd[0].control==legacy,
          "only the tagged GS control is owned while ordinary controls remain borrowed");
    Block *slab=block_of(p->owned_control);
    CHECK(slab && slab->live && slab->n==2*0x880,"tagged multi-stage program owns one bounded slab");
    CHECK(!memcmp(p->sd[1].control,a,0x880) && p->sd[1].data==sd[1].data,"complete control snapshot preserves borrowed code identity");
    memcpy(a,b,0x880);
    CHECK(!memcmp(p->sd[1].control,a_original,0x880),"caller control mutation cannot change an already bound program");
    CHECK(set(p,(int)p->nstage,p->sd),"setting from the program's own list safely prepares before release");
    CHECK(!memcmp(p->sd[1].control,a_original,0x880),"self-aliasing setter preserves the earlier mask");
    CHECK(!memcmp(a,b_original,0x880) && !memcmp(b,b_original,0x880) && !memcmp(code,code_original,0x90),
          "setter leaves source buffers unchanged beyond the explicit caller mutation");
    NVNshaderData one=stage(b,code_b);CHECK(set(p,1,&one),"second mask binds successfully");
    CHECK(!memcmp(p->sd[0].control,b_original,0x880),"B mask becomes the exact snapshot");
    one=stage(a_original,code);CHECK(set(p,1,&one),"return to first mask binds successfully");
    CHECK(!memcmp(p->sd[0].control,a_original,0x880),"A-B-A returns to the exact first control");
    rejected(p,-1,&one,"negative stage count rejects");rejected(p,7,&one,"excess stage count rejects");
    rejected(p,1,NULL,"missing nonempty stage list rejects");
    fail_alloc=1;rejected(p,1,&one,"snapshot allocation failure rejects atomically");
    uint8_t invalid[0x880];memcpy(invalid,a_original,0x880);
    nvn_geometry_control_put(invalid+0x810,NVN_GEOMETRY_CONTROL_NATIVE);
    NVNshaderData bad=stage(invalid,code);rejected(p,1,&bad,"native tag rejects without a native context contract");
    memcpy(invalid,a_original,0x880);nvn_geometry_control_put(invalid+0x808,2);
    rejected(p,1,&bad,"unknown tag version rejects");
    memcpy(invalid,a_original,0x880);nvn_geometry_control_put(invalid+0x714,1);
    rejected(p,1,&bad,"tagged non-GS stage rejects");
    memcpy(invalid,a_original,0x880);invalid[0x838]=1;
    rejected(p,1,&bad,"nonzero tag reserved byte rejects");
    // The first stage has already allocated its slab when the second fails.
    NVNshaderData partial[2]={one,bad};rejected(p,2,partial,"later invalid tag frees preparation and preserves the old program");
    NVNshaderData borrowed=stage(legacy,code);CHECK(set(p,1,&borrowed),"untagged ordinary control remains supported");
    CHECK(!p->owned_control && p->sd[0].control==legacy,"ordinary control is not snapshotted or owned");
    CHECK(set(p,0,NULL) && !p->nstage && !p->owned_control,"empty stage list commits without an allocation");
    NVNshaderData six[6];for(unsigned i=0;i<6;i++)six[i]=borrowed;
    six[2]=one;CHECK(set(p,6,six) && p->nstage==6,"maximum stage-list extent snapshots its one tagged stage");
    CHECK(!memcmp(p->sd[2].control,a_original,0x880),"tagged stage at a nonzero slab index is retained exactly");
    finalize(p);unsigned release_after=releases,free_after=frees;finalize(p);
    CHECK(releases==release_after && frees==free_after,"repeated finalization never releases or frees twice");
    for(size_t i=sizeof(prog_t);i<sizeof storage;i++)CHECK(storage[i]==0xa5,"program lifecycle preserves allocation-stride and outer canaries");
    CHECK(!live_blocks(),"all program-owned control slabs are released");
}
static void compact(void) {
#ifdef _WIN32
    SYSTEM_INFO system;GetSystemInfo(&system);size_t page=system.dwPageSize;
    uint8_t *mapping=VirtualAlloc(NULL,2*page,MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);
    if(!mapping)abort();DWORD old;
    if(!VirtualProtect(mapping+page,page,PAGE_NOACCESS,&old))abort();
#else
    size_t page=(size_t)sysconf(_SC_PAGESIZE);
    uint8_t *mapping=mmap(NULL,2*page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(mapping==MAP_FAILED || mprotect(mapping+page,page,PROT_NONE))abort();
#endif
    uint8_t *raw=mapping+page-0xd8,code[0xa0]={0};
    memset(raw,0,0xd8);
    nvn_geometry_control_put(raw,0x98761234u);nvn_geometry_control_put(raw+4,0xc9);
    nvn_geometry_control_put(raw+0x30,0xd8);nvn_geometry_control_put(raw+0x38,0x90);
    nvn_geometry_control_put(raw+0x40,0x90);nvn_geometry_control_put(raw+0x44,0xa0);
    nvn_geometry_control_put(code,1|(4<<5)|(1<<10));
    memset(code+0x90,0x5a,0x10);
    uint8_t raw_expected[0xd8],code_expected[0xa0];
    memcpy(raw_expected,raw,sizeof raw_expected);memcpy(code_expected,code,sizeof code_expected);
    prog_t p;initialize(&p);NVNshaderData sd=stage(raw,code);
    unsigned before=lowers;CHECK(set(&p,1,&sd),"compact NVN2 control survives a no-legacy-tail guard page");
    CHECK(lowers==before && p.sd[0].control!=raw && p.owned_control,"registration snapshots raw NVN2 without lowering");
    CHECK(!memcmp(p.sd[0].control,raw_expected,sizeof raw_expected) &&
          !memcmp((void *)(uintptr_t)p.sd[0].data,code_expected,sizeof code_expected),"raw snapshot includes inline constants");
    memset(raw,0xee,0xd8);memset(code,0xee,sizeof code);
    CHECK(!memcmp(p.sd[0].control,raw_expected,sizeof raw_expected) &&
          !memcmp((void *)(uintptr_t)p.sd[0].data,code_expected,sizeof code_expected),"caller mutation does not change owned shader inputs");
    CHECK(set(&p,1,p.sd),"raw snapshot can replace itself before old ownership is released");
    fail_alloc=1;rejected(&p,1,p.sd,"raw snapshot OOM is transactional");
    memcpy(raw,raw_expected,sizeof raw_expected);memcpy(code,code_expected,sizeof code_expected);
    nvn_geometry_control_put(raw+0x30,0x40);rejected(&p,1,&sd,"short NVN2 header rejects");
    nvn_geometry_control_put(raw+0x30,0xd8);nvn_geometry_control_put(raw+0x38,0x400001);
    rejected(&p,1,&sd,"oversized NVN2 code rejects");
    nvn_geometry_control_put(raw+0x38,0x90);nvn_geometry_control_put(raw+0x44,0x80);
    rejected(&p,1,&sd,"reversed inline constant range rejects");
    memcpy(raw,raw_expected,sizeof raw_expected);nvn_geometry_control_put(code,0);
    nvn_geometry_control_put(raw+0x30,0xb1);rejected(&p,1,&sd,"compute control ending at 0xb1 rejects");
    nvn_geometry_control_put(raw+0x30,0xb6);rejected(&p,1,&sd,"compute control missing image count rejects");
    nvn_geometry_control_put(raw+0x30,0xd8);nvn_geometry_control_put(raw+0x38,1);
    rejected(&p,1,&sd,"one-byte code cannot supply a stage discriminator");
#ifdef _WIN32
    NVNshaderData truncated=stage(mapping+page-8,code);
    nvn_geometry_control_put(mapping+page-8,0x98761234u);nvn_geometry_control_put(mapping+page-4,0xc9);
    rejected(&p,1,&truncated,"guard-page truncated header rejects without a fault");
    NVNshaderData no_control=stage(NULL,code), no_code=stage(raw,NULL);
    rejected(&p,1,&no_control,"null control rejects");rejected(&p,1,&no_code,"null code rejects");
#endif
    uint8_t *heap_ctl=malloc(sizeof raw_expected),*heap_code=malloc(sizeof code_expected);
    if(!heap_ctl || !heap_code)abort();
    memcpy(heap_ctl,raw_expected,sizeof raw_expected);memcpy(heap_code,code_expected,sizeof code_expected);
    NVNshaderData heap=stage(heap_ctl,heap_code);CHECK(set(&p,1,&heap),"heap-owned caller shader inputs snapshot");
    CHECK(p.sd[0].control!=heap_ctl && p.sd[0].data!=(uintptr_t)heap_code,"raw control and code ownership are independent of caller");
    free(heap_ctl);free(heap_code);
    CHECK(!memcmp(p.sd[0].control,raw_expected,sizeof raw_expected) &&
          !memcmp((void *)(uintptr_t)p.sd[0].data,code_expected,sizeof code_expected),"owned raw shader inputs survive caller free");
    finalize(&p);lower_success=0;
    // Reuse the writable first page for a canonical caller-owned control, then
    // remove its entire mapping. Only the program snapshot may be read after it.
    uint8_t caller_bytes[0x880];authored(mapping,code,0);memcpy(caller_bytes,mapping,sizeof caller_bytes);
    initialize(&p);sd=stage(mapping,code);
    CHECK(set(&p,1,&sd),"canonical control in a removable caller mapping binds");
#ifdef _WIN32
    CHECK(VirtualFree(mapping,0,MEM_RELEASE),"compact control mapping released");
#else
    CHECK(!munmap(mapping,2*page),"compact control mapping released");
#endif
    CHECK(p.owned_control && p.sd[0].control!=mapping && !memcmp(p.sd[0].control,caller_bytes,sizeof caller_bytes),
          "owned control remains readable and exact after the caller unmaps its storage");
    if(p.owned_control) {
        int ok=set(&p,(int)p.nstage,p.sd);
        CHECK(ok,"self-aliasing setter still succeeds after original caller storage is unmapped");
        if(ok)CHECK(!memcmp(p.sd[0].control,caller_bytes,sizeof caller_bytes),"unmapped-source replacement retains complete snapshot bytes");
    }
    finalize(&p);
}
int main(void) {
    (void)tracked_malloc; // Keep the allocator provider available in the borrowing omission control.
    lifecycle();compact();CHECK(allocations==frees && !live_blocks(),"all tracked slabs freed exactly once overall");
    CHECK(dumps && lowers==0,"registration dumps owned inputs without eager lowering");
    printf("geometry program controls: %u checks, %u failures; %u allocations, %u frees, %u backend releases\n",
           checks,failures,allocations,frees,releases);
    for(unsigned i=0;i<block_count;i++)free(blocks[i].p);
    return failures!=0;
}
'''
    return before + actual + after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=os.environ.get("CC") or shutil.which("clang"))
    parser.add_argument("--out", type=Path, default=ROOT / "build/checks/geometry-program-control")
    parser.add_argument("--controls", action="store_true", help="run safe CPU-only semantic omission controls")
    args = parser.parse_args()
    if not args.cc:
        parser.error("pass --cc")
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for kind in ("fixed", "borrow", "early-free") if args.controls else ("fixed",):
        c = args.out / (kind + ".c")
        c.write_text(source(kind))
        for opt in ("-O0", "-O2"):
            exe = args.out / (kind + opt + (".exe" if os.name == "nt" else ""))
            command = [args.cc, opt, "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "runtime"), str(c), "-o", str(exe)]
            run = subprocess.run(command, capture_output=True, text=True)
            (args.out / (kind + opt + "-compile.log")).write_text(run.stdout + run.stderr)
            if run.returncode:
                print(run.stdout + run.stderr)
                return run.returncode
            run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
            (args.out / (kind + opt + "-run.log")).write_text(run.stdout + run.stderr)
            expected = run.returncode == 0 if kind == "fixed" else run.returncode == 1
            if kind == "borrow":
                expected &= "caller control mutation cannot change an already bound program" in run.stderr
            elif kind == "early-free":
                expected &= "backend drains while the old owned snapshot is still live" in run.stderr
            results.append(dict(kind=kind, opt=opt, command=command, exit_code=run.returncode, expected=expected,
                                stdout=run.stdout, stderr=run.stderr))
            print(kind, opt, run.stdout.strip())
            if not expected:
                print(run.stderr)
                return 1
    report = dict(results=results, runtime_sha256={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (
        "runtime/nvn_api_obj.c", "runtime/nvn_obj.h", "runtime/nvn_geometry_control.h", "runtime/gfx_shader.c")},
        limitations=["CPU-only actual program functions with bounded backend and lowerer spies.",
                     "Tracked frees poison and quarantine slabs; no CPU UAF is invoked by omission controls.",
                     "Compact control uses two OS pages with the second inaccessible; no GPU/native lowering is executed."])
    (args.out / "results.json").write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
