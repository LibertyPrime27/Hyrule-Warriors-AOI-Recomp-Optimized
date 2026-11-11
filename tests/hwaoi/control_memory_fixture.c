// Actual NVN recording/control-memory lifecycle with authored commands and a synchronous guest-callback adapter.
// No modules, GPU, generated game code or process manipulation are involved.
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include "nvn_api.h"
#include "nvn_reclaim.h"
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
PROC(nvnCommandBufferBindTexture); PROC(nvnCommandBufferBindSeparateSampler); PROC(nvnCommandBufferBindImage);
PROC(nvnCommandBufferBindTexturesDeferred);
PROC(nvnCommandBufferCallCommands);
PROC(nvnCommandBufferBindUniformBuffer); PROC(nvnCommandBufferBindUniformBuffers); PROC(nvnCommandBufferBindStorageBuffer);
PROC(nvnCommandBufferBindVertexBuffer);
PROC(nvnCommandBufferDrawElements); PROC(nvnCommandBufferDrawElementsBaseVertex); PROC(nvnCommandBufferDrawElementsInstanced);
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
int nvn_reclaim_submit(void *p,uint64_t n,uint64_t b,void(*fn)(void*)){(void)p;(void)n;(void)b;(void)fn;return 0;}
void nvn_reclaim_stats(NvnReclaimStats *out){memset(out,0,sizeof *out);}
void rt_log(const char *fmt,...) {(void)fmt;}
uint64_t plat_now_ns(void) { return GetTickCount64()*1000000ull; }
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
    uint64_t handles[3]={11,22,33};
    CALL(nvnCommandBufferBindTexture,U(&command),0,127,UINT64_C(0x123456789abcdef0));
    CALL(nvnCommandBufferBindSeparateSampler,U(&command),4,7,0);
    CALL(nvnCommandBufferBindImage,U(&command),5,31,UINT64_MAX);
    CALL(nvnCommandBufferBindTexturesDeferred,U(&command),0,1,1,U(handles));
    CALL(nvnCommandBufferBindTexturesDeferred,U(&command),4,3,3,U(handles));
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
    const uint32_t ops[]={OP_BIND_TEX,OP_BIND_SAMP,OP_BIND_IMG,OP_BIND_TEX,OP_BIND_TEX};
    const int stages[]={0,4,5,0,4},slots[]={127,7,31,1,3};
    const uint64_t values[]={UINT64_C(0x123456789abcdef0),0,UINT64_MAX};
    const uint8_t *deferred_start=NULL;
    for(unsigned i=0;i<5;i++){
        const op_hdr *h=(const op_hdr*)cursor;NvnOpScratch scratch;
        CHECK(h->op==ops[i]&&h->size==(i<3?24:i==3?40:56));
        const p_bind_handles *b=nvn_op_normalize(h,&scratch);
        CHECK(b&&b->stage==stages[i]&&b->first==slots[i]&&b->n==(i==4?3:1));
        CHECK(b->src==(i<3?0:U(handles))&&b->h[0]==(i<3?values[i]:11));
        if(i==3)deferred_start=cursor;
        cursor+=h->size;
    }
    CHECK(((const op_hdr *)cursor)->op==OP_END&&nvn_handle_bytes(handle)==(uint64_t)(cursor+8-(const uint8_t *)(uintptr_t)handle));
    CHECK(((cmd_t *)&command)->hw_used==17*400+7*40+116); // same guest-visible usage as the previous fixed records
    handles[0]=44;handles[2]=66;nvn_submit_prepare(&handle,1);
    const p_bind_handles *deferred=(const p_bind_handles*)(deferred_start+8);
    CHECK(deferred->h[0]==44&&deferred->src==U(handles));
    deferred=(const p_bind_handles*)(deferred_start+40+8);
    CHECK(deferred->h[0]==44&&deferred->h[1]==22&&deferred->h[2]==66);
    memset(viewport,0,sizeof viewport);memset(state,0,sizeof state);
    CHECK(((const p_viewports *)((const uint8_t *)(uintptr_t)handle+32))->v[0][0]==.25f);
    replayed=nvn_submit_count();retire(&command,storage,sizeof storage);CHECK(!stats().copies&&!stats().zombies);
    CALL(nvnCommandBufferFinalize,U(&command));
}
static void immediate_submission(void) {
    NVNcommandBuffer command;uint8_t control[64];initialize(&command,control,sizeof control);
    CALL(nvnCommandBufferBeginRecording,U(&command));
    for(unsigned i=0;i<1000;i++)CALL(nvnCommandBufferBindTexture,U(&command),4,i%128,(uint64_t)i);
    uint64_t handle=CALL(nvnCommandBufferEndRecording,U(&command));
    SYSTEM_INFO info;GetSystemInfo(&info);size_t page=info.dwPageSize;
    void *protected_page=(void *)(((uintptr_t)handle+page-1)&~(uintptr_t)(page-1));
    CHECK((uintptr_t)protected_page>(uintptr_t)handle&&
          (uintptr_t)protected_page+page<(uintptr_t)handle+nvn_handle_bytes(handle));
    DWORD old;CHECK(VirtualProtect(protected_page,page,PAGE_NOACCESS,&old));
    // Immediate records have no submit-time CPU reads. This protects the body,
    // not the prefix/index; replay would still need the original readable body.
    CHECK(!nvn_submit_capture_fences(&handle,1));
    nvn_submit_prepare(&handle,1);
    uint64_t queued=0;nvn_submit_prepare_async(&handle,&queued,1);CHECK(queued==handle);
    CHECK(!nvn_submit_capture_fences(&handle,1));
    DWORD ignored;CHECK(VirtualProtect(protected_page,page,old,&ignored));
    const op_hdr *h=(const op_hdr *)(uintptr_t)(handle+8);NvnOpScratch scratch;
    const p_bind_handles *b=nvn_op_normalize(h,&scratch);CHECK(b&&b->h[0]==0&&!b->src&&b->n==1);
    replayed=nvn_submit_count();retire(&command,control,sizeof control);
    CHECK(!stats().copies&&!stats().zombies);CALL(nvnCommandBufferFinalize,U(&command));
}
static void inlined_deferred_submission(void) {
    NVNcommandBuffer command[2];uint8_t control[2][64];uint64_t source=11,handles[2];
    for(unsigned i=0;i<2;i++)initialize(&command[i],control[i],sizeof control[i]);
    CALL(nvnCommandBufferBeginRecording,U(&command[0]));
    CALL(nvnCommandBufferBindTexturesDeferred,U(&command[0]),4,7,1,U(&source));
    handles[0]=CALL(nvnCommandBufferEndRecording,U(&command[0]));
    CALL(nvnCommandBufferBeginRecording,U(&command[1]));
    CALL(nvnCommandBufferCallCommands,U(&command[1]),1,U(handles));
    handles[1]=CALL(nvnCommandBufferEndRecording,U(&command[1]));
    retire(&command[0],control[0],sizeof control[0]);CHECK(!nvn_handle_live(handles[0]));
    for(unsigned round=0;round<3;round++){
        source=44+round;nvn_submit_prepare(&handles[1],1);
        const p_bind_handles *b=(const p_bind_handles*)(uintptr_t)(handles[1]+16);
        CHECK(b->src==U(&source)&&b->h[0]==source&&b->n==1);
    }
    replayed=nvn_submit_count();retire(&command[1],control[1],sizeof control[1]);
    CHECK(!stats().copies&&!stats().zombies);
    for(unsigned i=0;i<2;i++)CALL(nvnCommandBufferFinalize,U(&command[i]));
}
static void buffer_records(void) {
    NVNcommandBuffer command;uint8_t control[64];initialize(&command,control,sizeof control);
    NVNbufferRange ranges[3]={{0,0},{UINT64_MAX-7,16},{UINT64_C(0x200000000),UINT64_C(0x900000000)}};
    CALL(nvnCommandBufferBeginRecording,U(&command));
    CALL(nvnCommandBufferBindUniformBuffer,U(&command),4,7,UINT64_C(0x123456789abcdef0),65536);
    CALL(nvnCommandBufferBindStorageBuffer,U(&command),5,31,UINT64_C(0x200000000),UINT64_C(0x100000000));
    CALL(nvnCommandBufferBindUniformBuffers,U(&command),0,1,3,U(ranges));
    CALL(nvnCommandBufferBindUniformBuffer,U(&command),UINT64_MAX,0,0,4);
    CALL(nvnCommandBufferBindVertexBuffer,U(&command),15,UINT64_MAX-31,UINT32_MAX);
    uint64_t handle=CALL(nvnCommandBufferEndRecording,U(&command));
    const p_bind_buf expected[]={{4,7,UINT64_C(0x123456789abcdef0),65536},{5,31,UINT64_C(0x200000000),UINT64_C(0x100000000)},
        {0,1,0,0},{0,2,UINT64_MAX-7,16},{0,3,UINT64_C(0x200000000),UINT64_C(0x900000000)},{-1,0,0,4}};
    const uint32_t sizes[]={24,32,24,24,32,32};const uint8_t *cursor=(const uint8_t*)(uintptr_t)(handle+8);
    memset(ranges,0,sizeof ranges);
    for(unsigned i=0;i<6;i++){
        const op_hdr *h=(const op_hdr*)cursor;NvnOpScratch scratch;
        CHECK(h->op==(i==1?OP_BIND_SSBO:OP_BIND_UBO)&&h->size==sizes[i]);
        const p_bind_buf *b=nvn_op_normalize(h,&scratch);CHECK(b&&!memcmp(b,&expected[i],sizeof *b));cursor+=h->size;
    }
    const op_hdr *vertex=(const op_hdr*)cursor;NvnOpScratch scratch;
    const p_bind_vbuf *v=nvn_op_normalize(vertex,&scratch);
    CHECK(vertex->op==OP_BIND_VBUF&&vertex->size==24&&v&&v->first==15&&v->n==1);
    CHECK(v->r[0].address==UINT64_MAX-31&&v->r[0].size==UINT32_MAX);cursor+=vertex->size;
    CHECK(((const op_hdr*)cursor)->op==OP_END&&nvn_handle_bytes(handle)==208);
    CHECK(((cmd_t*)&command)->hw_used==144);
    retire(&command,control,sizeof control);CHECK(!stats().copies);CALL(nvnCommandBufferFinalize,U(&command));
}
static void recorder_reinit_accounting(void) {
    NVNcommandBuffer original,copy;uint8_t control[2][64];initialize(&original,control[0],64);record(&original);
    CHECK(stats().record_buffers==1&&stats().record_bytes==65536);
    memcpy(&copy,&original,sizeof copy);
    CALL(nvnCommandBufferInitialize,U(&copy),0); // stale template bytes do not own the original allocation
    CHECK(stats().record_buffers==1&&stats().record_bytes==65536&&stats().record_reinits==1);
    CALL(nvnCommandBufferAddCommandMemory,U(&copy),0,0,65536);retire(&copy,control[1],64);record(&copy);
    CHECK(stats().record_buffers==2&&stats().record_bytes==131072&&stats().record_peak_bytes==131072);
    retire(&original,control[0],64);retire(&copy,control[1],64);
    CALL(nvnCommandBufferFinalize,U(&copy));CHECK(stats().record_buffers==1&&stats().record_bytes==65536);
    CALL(nvnCommandBufferFinalize,U(&original));CHECK(!stats().record_buffers&&!stats().record_bytes);
}
static void indexed_records(void) {
    NVNcommandBuffer command;uint8_t control[64];initialize(&command,control,sizeof control);
    CALL(nvnCommandBufferBeginRecording,U(&command));uint64_t address=UINT64_C(0x123456789abcdef0);
    CALL(nvnCommandBufferDrawElements,U(&command),4,1,3,address);
    CALL(nvnCommandBufferDrawElementsBaseVertex,U(&command),4,1,3,address,UINT64_MAX);
    CALL(nvnCommandBufferDrawElementsInstanced,U(&command),4,1,3,address,(uint64_t)-4,0,2);
    CALL(nvnCommandBufferDrawElementsInstanced,U(&command),4,1,3,address,(uint64_t)-4,7,2);
    uint64_t handle=CALL(nvnCommandBufferEndRecording,U(&command));const uint8_t *cursor=(const uint8_t*)(uintptr_t)(handle+8);
    const uint32_t sizes[]={24,32,32,40};const int bases[]={0,-1,-4,-4};
    for(unsigned i=0;i<4;i++){
        const op_hdr *h=(const op_hdr*)cursor;NvnOpScratch scratch;const p_draw_idx *r=nvn_op_normalize(h,&scratch);
        CHECK(h->op==OP_DRAW_IDX&&h->size==sizes[i]&&r&&r->prim==4&&r->itype==1&&r->count==3);
        CHECK(r->ibuf==address&&r->base_vertex==bases[i]&&r->base_instance==(i==3?7:0)&&r->instances==(i>=2?2:1));cursor+=h->size;
    }
    CHECK(((const op_hdr*)cursor)->op==OP_END&&nvn_handle_bytes(handle)==144&&((cmd_t*)&command)->hw_used==180);
    retire(&command,control,sizeof control);CALL(nvnCommandBufferFinalize,U(&command));
}
int main(int argc,char **argv) {
    CHECK(argc==2);const char *mode=argv[1];
    if(!strcmp(mode,"packed"))packed_records();
    else if(!strcmp(mode,"immediate-submit"))immediate_submission();
    else if(!strcmp(mode,"inlined-deferred"))inlined_deferred_submission();
    else if(!strcmp(mode,"buffer-records"))buffer_records();
    else if(!strcmp(mode,"reinit-accounting"))recorder_reinit_accounting();
    else if(!strcmp(mode,"indexed-records"))indexed_records();
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
    CHECK(!stats().record_buffers&&!stats().record_bytes&&stats().record_peak_bytes>=65536);
    CHECK(stats().record_reinits==(!strcmp(mode,"reinit-accounting")?1u:0u));
    puts("control-memory lifetime checks passed");return 0;
}
