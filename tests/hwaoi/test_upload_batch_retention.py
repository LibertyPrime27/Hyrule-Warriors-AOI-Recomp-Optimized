"""Retire production upload batches only after completion; bound spare objects."""
import os,re,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
CC=Path(os.environ.get("HWAOI_TOOLS", "F:") + '/llvm-mingw/bin/clang.exe')
DEPS=Path(os.environ.get('HWAOI_TEST_DEPS') or 'F:/TotKDependencies/runtime/deps')/'include'   # HWAOI_TEST_DEPS: a deps tree with include/
sys.path.insert(0,str(ROOT/'tests'))
from nvn2_compute_c0_test import function

def source():
    text=(ROOT/'runtime/vk_memory.c').read_text(encoding='utf-8')
    start=text.index('typedef struct { VkBuffer buf; VmaAllocation alloc; } Staging;')
    types=text[start:text.index('// The perf line',start)]
    constants='\n'.join(re.findall(r'^#define UP_(?:PAIRS|SPARES) \d+$',text,re.M))
    head=r'''
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "vk.h"
#define CHECK(x) do{if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
'''+constants+'\n'+types+r'''
static VkCommandPool cmd_pool=(VkCommandPool)(uintptr_t)77;
static VkQueryPool up_pool=(VkQueryPool)(uintptr_t)88;
static int up_pairs,up_free_pairs[UP_PAIRS],up_free_count;
static float up_period=1;
static volatile int64_t up_gpu_ns,up_timed;
static Batch *inflight,*spare;static int ninflight,nspare,cap_spare;
static uint64_t done_seq;static unsigned destroyed_fences,freed_commands,freed_buffers,waits;
static unsigned completed;
static int64_t plat_add64(volatile int64_t *p,int64_t v){*p+=v;return *p;}
static int64_t plat_inc64(volatile int64_t *p){return ++*p;}
void rt_fatal(const char *fmt,...){(void)fmt;abort();}
VKAPI_ATTR VkResult VKAPI_CALL vkGetFenceStatus(VkDevice d,VkFence f){(void)d;return (uintptr_t)f<=completed?VK_SUCCESS:VK_NOT_READY;}
VKAPI_ATTR VkResult VKAPI_CALL vkWaitForFences(VkDevice d,uint32_t n,const VkFence *f,VkBool32 a,uint64_t t){
 (void)d;(void)a;CHECK(n==1&&t==UINT64_MAX);completed=(unsigned)(uintptr_t)*f;waits++;return VK_SUCCESS;}
VKAPI_ATTR VkResult VKAPI_CALL vkGetQueryPoolResults(VkDevice d,VkQueryPool p,uint32_t first,uint32_t count,size_t bytes,void *data,VkDeviceSize stride,VkQueryResultFlags flags){
 (void)d;(void)p;(void)first;(void)stride;(void)flags;CHECK(count==2&&bytes==16);uint64_t out[2]={1,101};memcpy(data,out,16);return VK_SUCCESS;}
VKAPI_ATTR void VKAPI_CALL vkDestroyFence(VkDevice d,VkFence f,const VkAllocationCallbacks *a){(void)d;(void)a;CHECK((uintptr_t)f<=completed);destroyed_fences++;}
VKAPI_ATTR void VKAPI_CALL vkFreeCommandBuffers(VkDevice d,VkCommandPool p,uint32_t count,const VkCommandBuffer *b){
 (void)d;CHECK(p==cmd_pool&&count==1&&(uintptr_t)*b<=completed);freed_commands++;}
void vmaDestroyBuffer(VmaAllocator a,VkBuffer b,VmaAllocation m){(void)a;(void)b;(void)m;freed_buffers++;}
'''
    functions='\n'.join(function(text,name) for name in ('static void grow(void **arr, int *cap, size_t elem) {', 'static void free_staging(',
                       'static int upload_pair_take(', 'static void release_batch(', 'static void retire('))
    tail=r'''
int main(void){
 VkCtx context={0};const unsigned peak=UP_SPARES+24;inflight=calloc(peak,sizeof *inflight);up_pairs=(int)peak;
 for(unsigned i=0;i<peak;i++){
  Batch *b=&inflight[i];b->cb=(VkCommandBuffer)(uintptr_t)(i+1);b->fence=(VkFence)(uintptr_t)(i+1);
  b->seq=i+1;b->ts=(int)i;b->timed=1;b->stg=calloc(2,sizeof *b->stg);b->cap=b->nstg=2;
 }
 ninflight=(int)peak;retire(&context,0);CHECK(ninflight==(int)peak&&!nspare&&!freed_commands&&!freed_buffers);
 completed=UP_SPARES+3;retire(&context,0);CHECK(nspare==UP_SPARES&&ninflight==(int)peak-(int)completed);
 CHECK(done_seq==completed&&freed_commands==3&&destroyed_fences==3&&up_free_count==3);
 CHECK(freed_buffers==2*completed&&up_timed==completed&&up_gpu_ns==100*completed);
 retire(&context,1);CHECK(!ninflight&&nspare==UP_SPARES&&done_seq==peak);
 CHECK(freed_commands==24&&destroyed_fences==24&&up_free_count==24&&freed_buffers==2*peak);
 CHECK(waits==peak-(UP_SPARES+3));
 // Returned query pairs cannot alias the retained command buffers' pairs.
 unsigned used[UP_PAIRS]={0};for(int i=0;i<nspare;i++){CHECK(!used[spare[i].ts]);used[spare[i].ts]=1;}
 for(unsigned i=0;i<peak;i++){int pair=upload_pair_take();CHECK(pair>=0&&pair<UP_PAIRS&&!used[pair]);used[pair]=1;}
 CHECK(!up_free_count);up_pairs=UP_PAIRS;CHECK(upload_pair_take()==-1);
 for(int i=0;i<nspare;i++)release_batch(&context,&spare[i]);
 CHECK(freed_commands==peak&&destroyed_fences==peak);free(spare);free(inflight);
 puts("upload retention: completion ordering, bounded cache and query recycling passed");return 0;
}
'''
    return head+functions+tail

@unittest.skipUnless(os.name=='nt' and CC.is_file(),'Windows Clang required')
class UploadBatchRetentionTests(unittest.TestCase):
    def test_completed_batch_spares_are_bounded_and_timestamps_recycled(self):
        with tempfile.TemporaryDirectory(prefix='hwaoi-upload-retention-') as temp:
            folder=Path(temp);path=folder/'fixture.c';path.write_text(source(),encoding='utf-8')
            for opt in ('-O0','-O2'):
                exe=folder/'fixture.exe'
                result=subprocess.run([str(CC),opt,'-Wall','-Wextra','-Werror','-Wno-missing-field-initializers',
                    '-I',str(ROOT/'runtime'),'-I',str(DEPS),'-I',str(DEPS/'vma'),str(path),'-o',str(exe)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
                self.assertEqual(result.returncode,0,result.stderr)
                result=subprocess.run([str(exe)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
