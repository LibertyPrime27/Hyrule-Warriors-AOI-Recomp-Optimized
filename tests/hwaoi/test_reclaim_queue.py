"""Actual bounded CPU reclamation worker, ownership fallbacks and shutdown drain."""
import os,subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
CC=Path('F:/llvm-mingw/bin/clang.exe')


def source():
    before=r'''
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "platform/platform.h"
#include "nvn_reclaim.h"
#define CHECK(x) do{if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);ExitProcess(1);}}while(0)
static int enabled=1,thread_failure,malloc_failure;
static double headroom=2048;
static DWORD caller;static HANDLE entered,proceed;
static volatile LONG callbacks,metadata,threads,joins;
int cfg_bool(const char *name){CHECK(!strcmp(name,"TOTK_COPY_RECLAIM"));return enabled;}
void plat_commit_info(double *p,double *u,double *l){*p=0;*u=1024;*l=headroom<0?-1:1024+headroom;}
void plat_mutex_lock(PlatMutex *m){AcquireSRWLockExclusive((PSRWLOCK)m);}
void plat_mutex_unlock(PlatMutex *m){ReleaseSRWLockExclusive((PSRWLOCK)m);}
void plat_cond_wait(PlatCond *cv,PlatMutex *m){CHECK(SleepConditionVariableSRW((PCONDITION_VARIABLE)cv,(PSRWLOCK)m,INFINITE,0));}
void plat_cond_signal(PlatCond *cv){WakeConditionVariable((PCONDITION_VARIABLE)cv);}
void plat_cond_broadcast(PlatCond *cv){WakeAllConditionVariable((PCONDITION_VARIABLE)cv);}
void plat_thread_priority(int p){CHECK(p==PLAT_PRIO_BELOW_NORMAL);}
struct PlatThread{HANDLE handle;PlatThreadFn fn;void *arg;};
static DWORD WINAPI begin_thread(void *p){PlatThread *t=p;t->fn(t->arg);return 0;}
PlatThread *plat_thread_new(PlatThreadFn fn,void *arg,const char *name){
 CHECK(!strcmp(name,"command reclaim"));if(thread_failure)return NULL;
 PlatThread *t=calloc(1,sizeof *t);CHECK(t);t->fn=fn;t->arg=arg;
 t->handle=CreateThread(NULL,0,begin_thread,t,0,NULL);CHECK(t->handle);InterlockedIncrement(&threads);return t;
}
void plat_thread_join(PlatThread *t){CHECK(WaitForSingleObject(t->handle,10000)==WAIT_OBJECT_0);CloseHandle(t->handle);free(t);InterlockedIncrement(&joins);}
static void *batch_malloc(size_t n){if(malloc_failure)return NULL;void *p=malloc(n);if(p)InterlockedIncrement(&metadata);return p;}
static void batch_free(void *p){if(p){InterlockedDecrement(&metadata);free(p);}}
#define malloc batch_malloc
#define free batch_free
'''
    after=r'''
#undef malloc
#undef free
static void *payload(void){uint8_t *p=malloc(4096);CHECK(p);memset(p,0xa5,4096);return p;}
static void release(void *p){
 CHECK(GetCurrentThreadId()!=caller);SetEvent(entered);CHECK(WaitForSingleObject(proceed,10000)==WAIT_OBJECT_0);
 for(unsigned i=0;i<4096;i++)CHECK(((uint8_t*)p)[i]==0xa5);
 free(p);InterlockedIncrement(&callbacks);
}
int main(int argc,char **argv){
 CHECK(argc==2);caller=GetCurrentThreadId();const char *mode=argv[1];
 entered=CreateEvent(NULL,TRUE,FALSE,NULL);proceed=CreateEvent(NULL,TRUE,FALSE,NULL);CHECK(entered&&proceed);
 if(!strcmp(mode,"off"))enabled=0;
 if(!strcmp(mode,"low"))headroom=64;
 if(!strcmp(mode,"unknown"))headroom=-1;
 if(!strcmp(mode,"thread-failure"))thread_failure=1;
 if(!strcmp(mode,"malloc-failure"))malloc_failure=1;
 int rejected=!enabled||headroom<128||thread_failure||malloc_failure;
 void *first=payload();
 if(rejected){
  CHECK(!nvn_reclaim_submit(first,1,4096,release));CHECK(!callbacks&&((uint8_t*)first)[0]==0xa5);free(first);
  NvnReclaimStats s;nvn_reclaim_stats(&s);CHECK(!s.copies&&!s.bytes&&!s.batches&&!metadata);
 }else{
  int many=!strcmp(mode,"batches"),counts=!strcmp(mode,"counts");
  // Capacity values are synthetic reservations; payloads stay small/authored.
  uint64_t bytes=many||counts?4096:UINT64_C(1)<<30;
  uint64_t copies=counts?UINT64_C(1)<<20:1;
  CHECK(nvn_reclaim_submit(first,copies,bytes,release));CHECK(WaitForSingleObject(entered,10000)==WAIT_OBJECT_0);
  unsigned accepted=counts?1:many?64:2;
  for(unsigned i=1;i<accepted;i++)CHECK(nvn_reclaim_submit(payload(),1,bytes,release));
  NvnReclaimStats s;nvn_reclaim_stats(&s);CHECK(s.batches==accepted&&s.bytes==bytes*accepted&&!callbacks);
  void *extra=payload();CHECK(!nvn_reclaim_submit(extra,1,1,release));CHECK(((uint8_t*)extra)[0]==0xa5);free(extra);
  SetEvent(proceed);nvn_reclaim_shutdown();nvn_reclaim_stats(&s);
  CHECK(callbacks==(LONG)accepted&&!s.bytes&&!s.copies&&s.peak_bytes==bytes*accepted);
  CHECK(!metadata&&threads==1&&joins==1);
  extra=payload();CHECK(!nvn_reclaim_submit(extra,1,4096,release));free(extra);
 }
 nvn_reclaim_shutdown();CloseHandle(entered);CloseHandle(proceed);
 CHECK(!metadata);puts("reclaim queue: ownership, capacity, failure fallbacks and drain passed");return 0;
}
'''
    return before+(ROOT/'runtime/nvn_reclaim.c').read_text(encoding='utf-8')+after


@unittest.skipUnless(os.name=='nt' and CC.is_file(),'Windows Clang required')
class ReclaimQueueTests(unittest.TestCase):
    def test_real_worker_capacity_fallbacks_and_shutdown(self):
        with tempfile.TemporaryDirectory(prefix='hwaoi-reclaim-') as temp:
            folder=Path(temp);path=folder/'fixture.c';path.write_text(source(),encoding='utf-8')
            for opt in ('-O0','-O2'):
                exe=folder/'fixture.exe'
                result=subprocess.run([str(CC),opt,'-Wall','-Wextra','-Werror','-I',str(ROOT/'runtime'),str(path),'-o',str(exe)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                for mode in ('bytes','batches','counts','off','low','unknown','thread-failure','malloc-failure'):
                    with self.subTest(opt=opt,mode=mode):
                        result=subprocess.run([str(exe),mode],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
                        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
