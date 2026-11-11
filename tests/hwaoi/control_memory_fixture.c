// Actual NVN recording/control-memory lifecycle with authored commands and a synchronous guest-callback adapter.
// No modules, GPU, generated game code or process manipulation are involved.
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include "nvn_api.h"
#include "platform/platform.h"

#define PROC(n) void nvnp_##n(Cpu *c)
PROC(nvnCommandBufferInitialize); PROC(nvnCommandBufferFinalize);
PROC(nvnCommandBufferAddCommandMemory); PROC(nvnCommandBufferAddControlMemory);
PROC(nvnCommandBufferBeginRecording); PROC(nvnCommandBufferEndRecording);
PROC(nvnCommandBufferSetMemoryCallback); PROC(nvnCommandBufferSetMemoryCallbackData);
PROC(nvnCommandBufferSetControlMemoryCallbackEnabled);
PROC(nvnCommandBufferSetViewports); PROC(nvnCommandBufferSetPointSize);
PROC(nvnCommandBufferBindBlendState); PROC(nvnCommandBufferBindColorState);
PROC(nvnCommandBufferBindChannelMaskState); PROC(nvnCommandBufferBindMultisampleState);
PROC(nvnCommandBufferBindPolygonState); PROC(nvnCommandBufferBindDepthStencilState);
#define U(p) ((uint64_t)(uintptr_t)(p))
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1); } } while (0)
static uint64_t call(void (*fn)(Cpu *), const uint64_t args[8]) {
    Cpu c = {0}; memcpy(c.x,args,8*sizeof *args); fn(&c); return c.x[0];
}
#define CALL(n,...) call(nvnp_##n,(const uint64_t[8]){__VA_ARGS__})

unsigned rt_trace;
GfxStats gfx_counters;
static uint64_t replayed, callback_count, check_retired_handle;
static NVNcommandBuffer *callback_command;
static void *callback_storage;
static size_t callback_bytes;
static int callback_noop, callback_commands_only;
static const uint64_t callback_address=0x12345678, callback_data=0x5a1b2c3d;

void nvn_called(uint8_t *seen,const char *name) {(void)seen;(void)name;}
void nvn_mode_trace(unsigned kind,uint64_t cb,uint64_t h,uint32_t value,uint64_t submit) {
    (void)kind;(void)cb;(void)h;(void)value;(void)submit;
}
void plat_mutex_lock(PlatMutex *p) {AcquireSRWLockExclusive((PSRWLOCK)p);}
void plat_mutex_unlock(PlatMutex *p) {ReleaseSRWLockExclusive((PSRWLOCK)p);}
void plat_mutex_lock_shared(PlatMutex *p) {AcquireSRWLockShared((PSRWLOCK)p);}
void plat_mutex_unlock_shared(PlatMutex *p) {ReleaseSRWLockShared((PSRWLOCK)p);}
uint64_t gfx_replayed_submits(void) {return replayed;}
const GfxBackend *gfx_backend(void) {rt_fatal("unexpected GPU wait");return NULL;}
const char *cfg_get(const char *name) {(void)name;return NULL;}
void nvn_program_memory_stats(NvnMemoryStats *out) {(void)out;}
void rt_log(const char *fmt,...) {(void)fmt;}
int plat_mem_readable(uint64_t addr,uint64_t size) {(void)addr;(void)size;return 0;}
void rt_fatal(const char *fmt,...) {
    va_list ap;va_start(ap,fmt);vfprintf(stderr,fmt,ap);va_end(ap);fputc('\n',stderr);exit(73);
}
uint64_t rt_call(Cpu *c,uint64_t fn,int n,...) {
    (void)c;CHECK(fn==callback_address && n==4);
    va_list ap;va_start(ap,n);
    uint64_t object=va_arg(ap,uint64_t),event=va_arg(ap,uint64_t);
    uint64_t minimum=va_arg(ap,uint64_t),data=va_arg(ap,uint64_t);va_end(ap);
    CHECK(object==U(callback_command));
    CHECK(event==NVN_COMMAND_BUFFER_MEMORY_EVENT_OUT_OF_CONTROL_MEMORY && minimum==256 && data==callback_data);
    callback_count++;
    if(callback_noop)return 0;
    if(callback_commands_only)CALL(nvnCommandBufferAddCommandMemory,object,0,0,4096);
    else CALL(nvnCommandBufferAddControlMemory,object,U(callback_storage),callback_bytes);
    // Before EndRecording allocates the replacement, no allocator-address reuse can make an old numeric handle
    // alias the newly created handle. A queued retired copy still exists only as a protected zombie.
    if(check_retired_handle)CHECK(!nvn_handle_live(check_retired_handle));
    return 0;
}

static NvnMemoryStats stats(void) {NvnMemoryStats s;nvn_runtime_memory_stats(&s);return s;}
static void initialize(NVNcommandBuffer *command,void *storage,size_t bytes) {
    memset(command,0,sizeof *command);CALL(nvnCommandBufferInitialize,U(command),0);
    CALL(nvnCommandBufferAddCommandMemory,U(command),0,0,65536);
    if(storage || bytes)CALL(nvnCommandBufferAddControlMemory,U(command),U(storage),bytes);
}
static void configure_callback(NVNcommandBuffer *command,void *storage,size_t bytes) {
    callback_command=command;callback_storage=storage;callback_bytes=bytes;
    CALL(nvnCommandBufferSetMemoryCallback,U(command),callback_address);
    CALL(nvnCommandBufferSetMemoryCallbackData,U(command),callback_data);
}
static uint64_t record(NVNcommandBuffer *command) {
    CALL(nvnCommandBufferBeginRecording,U(command));
    uint64_t h=CALL(nvnCommandBufferEndRecording,U(command));
    CHECK(h && nvn_handle_live(h) && nvn_handle_bytes(h)==16);return h;
}
static void retire(NVNcommandBuffer *command,void *storage,size_t bytes) {
    CALL(nvnCommandBufferAddControlMemory,U(command),U(storage),bytes);
}

static void bounded_recycling(void) {
    NVNcommandBuffer command;uint8_t storage[256];initialize(&command,storage,sizeof storage);
    configure_callback(&command,storage,sizeof storage);
    uint64_t first=0;
    for(unsigned i=0;i<10000;i++){
        if(i && !(i%4))check_retired_handle=first;
        uint64_t h=record(&command);if(!(i%4))first=h;
        CHECK(callback_count==i/4 && stats().copies==i%4+1);
        CHECK(((cmd_t *)&command)->ctl_used==(i%4+1)*64);
        CHECK(stats().copy_bytes<=4*(16+40));
    }
    CHECK(callback_count==2499);retire(&command,storage,sizeof storage);
    CHECK(!stats().copies && !stats().copy_bytes && !stats().zombies);
    CALL(nvnCommandBufferFinalize,U(&command));
}
static void initial_callback(void) {
    NVNcommandBuffer command;uint8_t storage[256];initialize(&command,NULL,0);
    configure_callback(&command,storage,sizeof storage);record(&command);CHECK(callback_count==1);
    CHECK(((cmd_t *)&command)->ctl==storage && ((cmd_t *)&command)->ctl_used==64);
    retire(&command,storage,sizeof storage);CALL(nvnCommandBufferFinalize,U(&command));
}
static void exact_boundary(void) {
    NVNcommandBuffer command;uint8_t storage[128],next[256];initialize(&command,storage,sizeof storage);
    configure_callback(&command,next,sizeof next);uint64_t a=record(&command),b=record(&command);
    CHECK(!callback_count && ((cmd_t *)&command)->ctl_used==128);record(&command);CHECK(callback_count==1);
    CHECK(nvn_handle_live(a) && nvn_handle_live(b)); // a new nonoverlapping chunk does not invalidate cached handles
    CALL(nvnCommandBufferFinalize,U(&command));CHECK(nvn_handle_live(a) && nvn_handle_live(b));
    initialize(&command,NULL,0);retire(&command,storage,sizeof storage);retire(&command,next,sizeof next);
    CHECK(!stats().copies);CALL(nvnCommandBufferFinalize,U(&command));
}
static void partial_tail(void) {
    size_t page=65536;uint8_t *memory=malloc(3*page);CHECK(memory);
    uintptr_t aligned=((uintptr_t)memory+page-1)&~(uintptr_t)(page-1);
    uint8_t *control=(uint8_t *)(aligned+page-16);
    NVNcommandBuffer command;initialize(&command,control,64);uint64_t h=record(&command);
    CHECK(nvn_handle_live(h));retire(&command,control+32,16); // overlaps only the tail, in the next index page
    CHECK(!nvn_handle_live(h) && !stats().copies);
    CALL(nvnCommandBufferFinalize,U(&command));free(memory);
}
static void pending_replay(void) {
    NVNcommandBuffer command;uint8_t storage[64];initialize(&command,storage,sizeof storage);
    uint64_t old=record(&command);nvn_submit_prepare(&old,1);CHECK(nvn_submit_count()==1);
    configure_callback(&command,storage,sizeof storage);check_retired_handle=old;
    uint64_t replacement=record(&command);NvnMemoryStats s=stats();
    CHECK(callback_count==1 && s.copies==1 && s.zombies==1 && s.zombie_bytes==56);
    CHECK(!nvn_handle_live(old) && ((const op_hdr *)(uintptr_t)old)->op==OP_BEGIN);
    CHECK(nvn_handle_live(replacement));replayed=nvn_submit_count();retire(&command,storage,sizeof storage);
    s=stats();CHECK(!s.copies && !s.zombies && !s.zombie_bytes);
    CALL(nvnCommandBufferFinalize,U(&command));
}
static void packed_records(void) {
    NVNcommandBuffer command;uint8_t storage[64];initialize(&command,storage,sizeof storage);
    float viewport[64];for(unsigned i=0;i<64;i++)viewport[i]=(float)i+.25f;
    uint8_t state[24];for(unsigned i=0;i<sizeof state;i++)state[i]=(uint8_t)(0x31+i);
    void (*setters[])(Cpu *)={nvnp_nvnCommandBufferBindBlendState,nvnp_nvnCommandBufferBindColorState,
        nvnp_nvnCommandBufferBindChannelMaskState,nvnp_nvnCommandBufferBindMultisampleState,
        nvnp_nvnCommandBufferBindPolygonState,nvnp_nvnCommandBufferBindDepthStencilState};
    const size_t lengths[]={sizeof(blend_st),sizeof(color_st),sizeof(chmask_st),sizeof(ms_st),sizeof(poly_st),sizeof(ds_st)};
    CALL(nvnCommandBufferBeginRecording,U(&command));
    for(unsigned n=0;n<=16;n++)CALL(nvnCommandBufferSetViewports,U(&command),16-n,n,U(viewport));
    for(unsigned kind=0;kind<6;kind++)call(setters[kind],(const uint64_t[8]){U(&command),U(state)});
    Cpu cpu={0};cpu.x[0]=U(&command);float point=2.5f;memcpy(&cpu.v[0],&point,sizeof point);
    nvnp_nvnCommandBufferSetPointSize(&cpu);
    uint64_t handle=CALL(nvnCommandBufferEndRecording,U(&command));
    const uint8_t *cursor=(const uint8_t *)(uintptr_t)handle+8;
    for(unsigned n=0;n<=16;n++){
        const op_hdr *h=(const op_hdr *)cursor;CHECK(h->op==OP_VIEWPORTS&&h->size==16+24*n);
        const p_viewports *v=(const p_viewports *)(cursor+8);CHECK(v->first==(int)(16-n)&&v->n==(int)n);
        for(unsigned i=0;i<n;i++)CHECK(!memcmp(v->v[i],viewport+4*i,4*sizeof(float)));
        cursor+=h->size;
    }
    for(unsigned kind=0;kind<=6;kind++){
        const op_hdr *h=(const op_hdr *)cursor;size_t n=kind<6?lengths[kind]:sizeof point;
        CHECK(h->op==OP_BIND_STATE&&h->size==((12+n+7)&~(size_t)7));
        const p_bind_state *b=(const p_bind_state *)(cursor+8);CHECK(b->kind==(int)kind);
        CHECK(!memcmp(b->bytes,kind<6?(const void *)state:(const void *)&point,n));cursor+=h->size;
    }
    CHECK(((const op_hdr *)cursor)->op==OP_END&&nvn_handle_bytes(handle)==(uint64_t)(cursor+8-(const uint8_t *)(uintptr_t)handle));
    CHECK(((cmd_t *)&command)->hw_used==17*400+7*40); // same guest-visible usage as the previous fixed records
    memset(viewport,0,sizeof viewport);memset(state,0,sizeof state);
    CHECK(((const p_viewports *)((const uint8_t *)(uintptr_t)handle+32))->v[0][0]==.25f);
    retire(&command,storage,sizeof storage);CHECK(!stats().copies);
    CALL(nvnCommandBufferFinalize,U(&command));
}
int main(int argc,char **argv) {
    CHECK(argc==2);const char *mode=argv[1];
    if(!strcmp(mode,"packed"))packed_records();
    else if(!strcmp(mode,"recycling"))bounded_recycling();
    else if(!strcmp(mode,"initial"))initial_callback();
    else if(!strcmp(mode,"boundary"))exact_boundary();
    else if(!strcmp(mode,"tail"))partial_tail();
    else if(!strcmp(mode,"pending"))pending_replay();
    else {
        NVNcommandBuffer command;uint8_t storage[256];initialize(&command,storage,64);
        if(!strcmp(mode,"overflow-add"))retire(&command,(void *)(UINTPTR_MAX-31),64);
        else if(!strcmp(mode,"null-add"))retire(&command,NULL,64);
        else if(!strcmp(mode,"short")){retire(&command,storage,63);record(&command);}
        else if(!strcmp(mode,"overflow-end")){
            ((cmd_t *)&command)->ctl=(void *)(UINTPTR_MAX-31);record(&command);
        }else if(!strcmp(mode,"missing")){record(&command);record(&command);}
        else if(!strcmp(mode,"absent")){retire(&command,NULL,0);record(&command);}
        else if(!strcmp(mode,"disabled")){
            configure_callback(&command,storage,sizeof storage);
            CALL(nvnCommandBufferSetControlMemoryCallbackEnabled,U(&command),0);record(&command);record(&command);
        }else if(!strcmp(mode,"noop") || !strcmp(mode,"short-callback") || !strcmp(mode,"wrong-memory")){
            configure_callback(&command,storage,!strcmp(mode,"short-callback")?63:sizeof storage);
            callback_noop=!strcmp(mode,"noop");callback_commands_only=!strcmp(mode,"wrong-memory");
            record(&command);record(&command);
        }else CHECK(0);
        CHECK(0); // every failure mode must stop visibly before inventing a handle or reusing the first slot
    }
    puts("control-memory lifetime checks passed");return 0;
}
