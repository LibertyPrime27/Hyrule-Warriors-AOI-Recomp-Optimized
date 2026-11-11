// SPDX-License-Identifier: MIT
// Original synthetic NVN2 inputs; production codec/setter are inserted below.
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include "nvn_api.h"
#include "nvn_geometry_control.h"
#include "nvn_program_source.h"
#include "platform/platform.h"
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "line %d: %s\n", __LINE__, #x); ExitProcess(1); } } while (0)
#undef NVN_LOG
#define NVN_LOG(...) ((void)0)
static int enabled=1, fail_cache_alloc;
static int prefetch_enabled=1, async_enabled, lower_failure;
static unsigned lowers, prefetches;
static const uint8_t *expected_code;
static size_t expected_bytes;
static uint8_t lowered_control[0x880];
#define NVN_STAGE_CS 5
static unsigned checks, releases;
typedef struct { void *pointer; size_t bytes; } Allocation;
static Allocation allocations[4096];
static void *tracked_malloc(size_t bytes) {
    void *p=malloc(bytes); CHECK(p);
    for (unsigned i=0;i<4096;i++) if (!allocations[i].pointer) {
        allocations[i]=(Allocation){p,bytes}; return p;
    }
    abort();
}
static void *cache_malloc(size_t bytes) {
    if (fail_cache_alloc>0 && !--fail_cache_alloc) return NULL;
    return tracked_malloc(bytes);
}
static void tracked_free(void *p) {
    if (!p) return;
    for (unsigned i=0;i<4096;i++) if (allocations[i].pointer==p) {
        allocations[i]=(Allocation){0}; free(p); return;
    }
    CHECK(!"release of an unowned/already released buffer");
}
int cfg_bool(const char *key) { CHECK(!strcmp(key,"TOTK_SOURCE_COMPRESS")); return enabled; }
const char *cfg_value(const char *key) { CHECK(!strcmp(key,"TOTK_XLATE_PREFETCH")); return prefetch_enabled?"1":"0"; }
int vk_pipeline_async_on(void) { return async_enabled; }
int nvn2_lower(const uint8_t *control,const uint8_t *code,const uint8_t **out_control,const uint8_t **out_code) {
    (void)control;
    unsigned complete_allocation=0;
    for(unsigned i=0;i<4096;i++) complete_allocation+=allocations[i].pointer==code && allocations[i].bytes>=expected_bytes;
    // Check ownership before touching the complete payload. The omission
    // control fails here safely, without an out-of-bounds read of the prefix.
    CHECK(complete_allocation==1);
    CHECK(expected_code && !memcmp(code,expected_code,expected_bytes)); lowers++;
    if(lower_failure) return 0;
    uint32_t stage=NVN_STAGE_CS,size=(uint32_t)expected_bytes;
    memcpy(lowered_control+0x714,&stage,4); memcpy(lowered_control+0x704,&size,4);
    for(unsigned k=0;k<3;k++) { uint32_t local=k+3; memcpy(lowered_control+0x748+4*k,&local,4); }
    *out_control=lowered_control; *out_code=code; return 1;
}
int gfx_shader_prefetch(int stage,const uint8_t *control,const uint8_t *code,uint64_t bytes,const GfxShaderAnswers *answers) {
    CHECK(stage==GFX_STAGE_CS && control==lowered_control && bytes==expected_bytes);
    CHECK(!memcmp(code,expected_code,(size_t)bytes));
    for(unsigned k=0;k<3;k++) CHECK(answers->local[k]==k+3);
    for(unsigned k=0;k<sizeof answers->tex_type;k++) CHECK(answers->tex_type[k]==GFX_TEX_BUFFER);
    prefetches++; return 1;
}
void rt_log(const char *format,...) { (void)format; }
void rt_fatal(const char *format,...) {
    va_list ap; va_start(ap,format); vfprintf(stderr,format,ap); va_end(ap); ExitProcess(73);
}
void plat_mutex_lock(PlatMutex *m) { AcquireSRWLockExclusive((PSRWLOCK)m); }
void plat_mutex_unlock(PlatMutex *m) { ReleaseSRWLockExclusive((PSRWLOCK)m); }
void plat_mutex_lock_shared(PlatMutex *m) { AcquireSRWLockShared((PSRWLOCK)m); }
void plat_mutex_unlock_shared(PlatMutex *m) { ReleaseSRWLockShared((PSRWLOCK)m); }
struct PlatThread { HANDLE handle; PlatThreadFn fn; void *arg; };
static DWORD WINAPI begin_thread(void *p) { PlatThread *t=p; t->fn(t->arg); return 0; }
PlatThread *plat_thread_new(PlatThreadFn fn,void *arg,const char *name) {
    (void)name; PlatThread *t=malloc(sizeof *t); CHECK(t); t->fn=fn; t->arg=arg;
    t->handle=CreateThread(NULL,0,begin_thread,t,0,NULL); CHECK(t->handle); return t;
}
void plat_thread_join(PlatThread *t) { CHECK(WaitForSingleObject(t->handle,10000)==WAIT_OBJECT_0); CloseHandle(t->handle); free(t); }
int plat_mem_read(uint64_t address,void *out,size_t bytes) {
    SIZE_T read=0; return ReadProcessMemory(GetCurrentProcess(),(void *)(uintptr_t)address,out,bytes,&read) && read==bytes;
}
uint64_t nvn_new_serial(void) { static uint64_t next; return ++next; }
void gfx_shader_nvn2_dump(const uint8_t *control,const uint8_t *code) { (void)control; (void)code; }
static void program_memory_allocated(const void *p,size_t bytes) { (void)p; (void)bytes; }
static void program_memory_released(const void *p) { (void)p; }
static void backend_release(const prog_t *program) {
    CHECK(program->serial && program->owned_control);
    unsigned found=0;
    for (unsigned i=0;i<4096;i++) found+=allocations[i].pointer==program->owned_control;
    CHECK(found==1); releases++;
}
static const GfxBackend backend={.program_release=backend_release};
const GfxBackend *gfx_backend(void) { return &backend; }
#define malloc cache_malloc
#define free tracked_free
@SOURCE_CODEC@
#undef malloc
#define malloc tracked_malloc
@PROGRAM_PREFETCH@
@PROGRAM_FUNCTIONS@
#undef malloc
#undef free

typedef struct { const uint8_t *key,*expected,*result; size_t bytes; unsigned failures; } ExpandTask;
static void expand_task(void *argument) {
    ExpandTask *test=argument;
    for (unsigned i=0;i<1000;i++) {
        const uint8_t *source=nvn_program_source(test->key);
        if ((test->result && test->result!=source) || memcmp(source,test->expected,test->bytes)) test->failures++;
        test->result=source;
    }
}
static NvnProgramSourceStats stats(void) { NvnProgramSourceStats out; nvn_program_source_stats(&out); return out; }
static void empty_stats(void) {
    NvnProgramSourceStats s=stats(); CHECK(!s.blocks && !s.original_bytes && !s.retained_bytes && !s.auxiliary_bytes);
    size_t retained=0;
    for (unsigned i=0;i<4096;i++) if (allocations[i].pointer) retained+=allocations[i].bytes;
    CHECK(retained==s.index_bytes); checks++;
}
int main(int argc,char **argv) {
    CHECK(argc==2); enabled=strcmp(argv[1],"off")!=0;
    enum { CONTROL=0xd8, CODE=16384 };
    uint8_t control[CONTROL]={0}, code[CODE]; uint32_t value=0x98761234;
    memcpy(control,&value,4); value=0xc9; memcpy(control+4,&value,4);
    value=CONTROL; memcpy(control+0x30,&value,4); value=CODE; memcpy(control+0x38,&value,4);
    for (unsigned i=0;i<CODE;i++) code[i]=(uint8_t)(i%17);
    value=0x481; memcpy(code,&value,4);
    NVNshaderData input={(uintptr_t)code,control}; prog_t program={0};
    if(!strncmp(argv[1],"prefetch",8)) {
        prefetch_enabled=strcmp(argv[1],"prefetch-off")!=0;
        async_enabled=strcmp(argv[1],"prefetch-sync")!=0;
        lower_failure=!strcmp(argv[1],"prefetch-fail");
        int graphics=!strcmp(argv[1],"prefetch-graphics");
        if(!graphics) { value=0; memcpy(code,&value,4); }
        expected_code=code; expected_bytes=CODE;
        CHECK(prog_set_shaders(&program,1,&input));
        unsigned active=prefetch_enabled && async_enabled && !graphics;
        CHECK(lowers==active && prefetches==(active && !lower_failure));
        CHECK(stats().blocks==1 && (!active || stats().auxiliary_bytes==CODE));
        CHECK(!memcmp(nvn_program_source((uint8_t *)(uintptr_t)program.sd[0].data),code,CODE));
        prog_release(&program); empty_stats();
        puts("compressed registration/prefetch integration passed"); return 0;
    }
    if (!enabled) {
        CHECK(prog_set_shaders(&program,1,&input)); CHECK(!memcmp((void *)(uintptr_t)program.sd[0].data,code,CODE));
        CHECK(nvn_program_source((uint8_t *)(uintptr_t)program.sd[0].data)==(uint8_t *)(uintptr_t)program.sd[0].data);
        prog_release(&program); empty_stats(); return 0;
    }
    for (int fail=1;fail<=5;fail++) {
        fail_cache_alloc=fail; CHECK(prog_set_shaders(&program,1,&input));
        CHECK(!memcmp((void *)(uintptr_t)program.sd[0].data,code,CODE)); prog_release(&program); empty_stats();
    }
    fail_cache_alloc=0; CHECK(prog_set_shaders(&program,1,&input));
    uintptr_t key=(uintptr_t)program.sd[0].data; ProgramSource *entry=nvn_hm_get(&sources,&key);
    CHECK(entry && !entry->expanded && stats().retained_bytes<stats().original_bytes);
    CHECK(!memcmp(program.sd[0].control,control,CONTROL) && !memcmp((void *)key,code,SOURCE_PREFIX));
    if (!strcmp(argv[1],"corrupt")) { entry->packed[entry->packed_bytes-1]^=0x40; nvn_program_source((uint8_t *)key); CHECK(0); }
    if (!strcmp(argv[1],"expand-oom")) { fail_cache_alloc=1; nvn_program_source((uint8_t *)key); CHECK(0); }
    ExpandTask tasks[8]; PlatThread *threads[8];
    for (unsigned i=0;i<8;i++) {
        tasks[i]=(ExpandTask){.key=(uint8_t *)key,.expected=code,.bytes=CODE};
        threads[i]=plat_thread_new(expand_task,&tasks[i],"program source");
    }
    for (unsigned i=0;i<8;i++) plat_thread_join(threads[i]);
    for (unsigned i=0;i<8;i++) CHECK(!tasks[i].failures && tasks[i].result==tasks[0].result);
    CHECK(!entry->packed && !entry->packed_bytes && stats().auxiliary_bytes==CODE);
    prog_t snapshot=program; CHECK(nvn_program_source((uint8_t *)(uintptr_t)snapshot.sd[0].data)==tasks[0].result);
    prog_release(&program); empty_stats();
    CHECK(prog_set_shaders(&program,1,&input));
    CHECK(prog_set_shaders(&program,1,program.sd));
    CHECK(!memcmp(nvn_program_source((uint8_t *)(uintptr_t)program.sd[0].data),code,CODE));
    prog_t before=program; NVNshaderData invalid={0};
    CHECK(!prog_set_shaders(&program,1,&invalid) && !memcmp(&before,&program,sizeof program));
    prog_release(&program); empty_stats();
    CHECK(prog_set_shaders(&program,1,&input));
    NVNshaderData aliases[2]={program.sd[0],program.sd[0]};
    CHECK(prog_set_shaders(&program,2,aliases)); CHECK(!stats().blocks);
    CHECK(!memcmp((void *)(uintptr_t)program.sd[0].data,code,CODE));
    CHECK(!memcmp((void *)(uintptr_t)program.sd[1].data,code,CODE));
    prog_release(&program); empty_stats();
    prog_t live[192]={0};
    for (unsigned i=0;i<192;i++) CHECK(prog_set_shaders(&live[i],1,&input));
    CHECK(stats().blocks==192);
    for (unsigned i=0;i<192;i++) {
        unsigned at=i*73%192;
        if (i%2) CHECK(!memcmp(nvn_program_source((uint8_t *)(uintptr_t)live[at].sd[0].data),code,CODE));
        prog_release(&live[at]);
    }
    empty_stats();
    // Fill only index metadata after preparing an ordinary raw program. No
    // lookup may inspect the deliberately absent synthetic full-table storage.
    fail_cache_alloc=1; CHECK(prog_set_shaders(&program,1,&input)); fail_cache_alloc=0;
    nvn_hmap saved=sources; sources=(nvn_hmap){.klen=sizeof(uintptr_t),.cap=SOURCE_MAX_CAP,.n=SOURCE_MAX_CAP*3/4};
    CHECK(!nvn_program_source_pack(&program,CONTROL+CODE));
    CHECK(!memcmp((void *)(uintptr_t)program.sd[0].data,code,CODE));
    sources=saved; prog_release(&program); empty_stats();
    uint32_t random=0x12345678;
    for (unsigned i=0;i<CODE;i++) { random^=random<<13; random^=random>>17; random^=random<<5; code[i]=(uint8_t)random; }
    value=0x481; memcpy(code,&value,4);
    CHECK(prog_set_shaders(&program,1,&input)); CHECK(!stats().blocks);
    CHECK(!memcmp((void *)(uintptr_t)program.sd[0].data,code,CODE)); prog_release(&program); empty_stats();
    NVNshaderData multiple[2]={input,input}; CHECK(prog_set_shaders(&program,2,multiple)); CHECK(!stats().blocks);
    CHECK(!memcmp((void *)(uintptr_t)program.sd[1].data,code,CODE)); prog_release(&program); empty_stats();
    CHECK(releases>192); nvn_program_source_stats(NULL);
    printf("program source: exact bytes, concurrency, alias replacement, bounded-index/OOM fallbacks, checksum and ownership passed (%u cleanup checks)\n",checks);
    return 0;
}
