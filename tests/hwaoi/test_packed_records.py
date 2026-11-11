"""Lossless host record semantics, legacy compatibility and malformed bounds."""
import os,subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
CC=Path('F:/llvm-mingw/bin/clang.exe')
CODE=r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "nvn_obj.h"
#define CHECK(x) do{checks++;if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
static unsigned checks;
typedef union{uint64_t alignment;uint8_t bytes[512];} Packet;
static uint32_t aligned(size_t n){return (uint32_t)((n+7)&~(size_t)7);}
static void rt_cases(void){
 Packet packet;NvnOpScratch scratch;
 for(unsigned n=0;n<=8;n++)for(unsigned mask=0;mask<(1u<<n);mask++)for(unsigned depth=0;depth<4;depth++){
  p_set_rt expected;memset(&expected,0,sizeof expected);expected.n=(int)n;
  expected.depth=depth&1?(const tex_t*)(uintptr_t)0x5000:NULL;
  expected.has_dview=(depth>>1)&1;if(expected.has_dview)memset(&expected.dview,0x37,sizeof expected.dview);
  for(unsigned i=0;i<n;i++){
   expected.color[i]=i&1?NULL:(const tex_t*)(uintptr_t)(0x1000+128*i);
   expected.has_cview[i]=(mask>>i)&1;
   if(expected.has_cview[i])memset(&expected.cview[i],(int)(0x40+i),sizeof(view_t));
  }
  memset(&packet,0,sizeof packet);op_hdr *h=(op_hdr*)packet.bytes;h->op=OP_SET_RT;h->size=8+sizeof expected;
  memcpy(packet.bytes+8,&expected,sizeof expected);
  const void *legacy=nvn_op_normalize(h,&scratch);CHECK(legacy&&!memcmp(legacy,&expected,sizeof expected));
  size_t size=nvn_rt_pack(NULL,&expected);CHECK(size&&aligned(size)<=sizeof expected);
  memset(&packet,0,sizeof packet);h->op=OP_SET_RT;h->size=8+aligned(size);
  CHECK(nvn_rt_pack(packet.bytes+8,&expected)==size);memset(&scratch,0xcd,sizeof scratch);
  const void *actual=nvn_op_normalize(h,&scratch);CHECK(actual&&!memcmp(actual,&expected,sizeof expected));
  uint32_t full=h->size;
  for(uint32_t short_size=8;short_size<full;short_size+=8){h->size=short_size;CHECK(!nvn_op_normalize(h,&scratch));}
  h->size=full;uint32_t flags;memcpy(&flags,packet.bytes+8,4);flags|=1u<<29;memcpy(packet.bytes+8,&flags,4);
  CHECK(!nvn_op_normalize(h,&scratch));
 }
}
static void array_cases(void){
 const uint32_t ops[]={OP_BIND_VATTR,OP_BIND_VSTREAM,OP_BIND_VBUF};const size_t sizes[]={4,8,16};
 for(unsigned kind=0;kind<3;kind++)for(unsigned n=0;n<=16;n++)for(unsigned first=0;first<=16-n;first++)for(unsigned seed=0;seed<12;seed++){
  uint8_t values[256]={0};unsigned mask=(seed?seed==1?~0u:seed*0x9e3779b9u:0)&((1u<<n)-1);size_t item=sizes[kind];
  for(unsigned i=0;i<n;i++)if(mask&(1u<<i))for(unsigned j=0;j<item;j++)values[i*item+j]=(uint8_t)((i*17+j+seed)|1);
  Packet packet;memset(&packet,0,sizeof packet);op_hdr *h=(op_hdr*)packet.bytes;h->op=ops[kind];
  size_t size=nvn_array_pack(NULL,(int)first,(int)n,values,item);CHECK(size&&aligned(size)<=aligned(8+n*item));
  CHECK(nvn_array_pack(packet.bytes+8,(int)first,(int)n,values,item)==size);h->size=8+aligned(size);
  NvnOpScratch scratch;memset(&scratch,0xcd,sizeof scratch);const uint8_t *decoded=nvn_op_normalize(h,&scratch);CHECK(decoded);
  int32_t fields[2];memcpy(fields,decoded,8);CHECK(fields[0]==(int)first&&fields[1]==(int)n);
  CHECK(!memcmp(decoded+8,values,n*item));
  uint32_t full=h->size;
  for(uint32_t short_size=8;short_size<full;short_size+=8){h->size=short_size;CHECK(!nvn_op_normalize(h,&scratch));}
  h->size=full;uint32_t invalid=NVN_RECORD_PACKED|17,first_flags;memcpy(&first_flags,packet.bytes+8,4);
  memcpy(packet.bytes+(ops[kind]==OP_BIND_VBUF&&(first_flags&NVN_RECORD_PACKED)?8:12),&invalid,4);CHECK(!nvn_op_normalize(h,&scratch));
 }
}
static void independent_wire_cases(void){
 // These bytes are authored independently of either encoder.
 Packet packet;memset(&packet,0,sizeof packet);op_hdr *h=(op_hdr*)packet.bytes;NvnOpScratch scratch;
 h->op=OP_SET_RT;h->size=48;uint32_t flags=UINT32_C(0x80000011);
 const tex_t *depth=(const tex_t*)(uintptr_t)0x7000,*color=(const tex_t*)(uintptr_t)0x9000;view_t view;memset(&view,0x6a,sizeof view);
 memcpy(packet.bytes+8,&flags,4);memcpy(packet.bytes+16,&depth,8);memcpy(packet.bytes+24,&color,8);memcpy(packet.bytes+32,&view,16);
 const p_set_rt *rt=nvn_op_normalize(h,&scratch);CHECK(rt&&rt->n==1&&rt->depth==depth&&rt->color[0]==color);
 CHECK(rt->has_cview[0]&&!rt->has_dview&&!memcmp(&rt->cview[0],&view,sizeof view));
 memset(&packet,0,sizeof packet);h->op=OP_BIND_VSTREAM;h->size=32;flags=UINT32_C(0x80000010);uint32_t present=1u<<7;
 vs_st stream={48,1};memcpy(packet.bytes+12,&flags,4);memcpy(packet.bytes+16,&present,4);memcpy(packet.bytes+24,&stream,8);
 const p_bind_vstream *s=nvn_op_normalize(h,&scratch);CHECK(s&&s->first==0&&s->n==16);
 for(unsigned i=0;i<16;i++)CHECK(s->st[i].stride==(i==7?48u:0u)&&s->st[i].divisor==(i==7?1u:0u));
 h->size=24;CHECK(!nvn_op_normalize(h,&scratch));h->size=32;
 uint32_t bad=1;memcpy(packet.bytes+20,&bad,4);CHECK(!nvn_op_normalize(h,&scratch));
 h->op=OP_COUNT_;CHECK(!nvn_op_normalize(h,&scratch));
}
static void handle_cases(void){
 const unsigned ops[]={OP_BIND_TEX,OP_BIND_SAMP,OP_BIND_IMG};
 const uint64_t values[]={0,1,UINT64_C(0xffffffffffffffff),UINT64_C(0x123456789abcdef0)};
 const int slots[]={-1,0,31,127,INT32_MAX};
 for(unsigned kind=0;kind<3;kind++)for(int stage=0;stage<6;stage++)for(unsigned slot=0;slot<5;slot++)for(unsigned v=0;v<4;v++){
  Packet packet={0};op_hdr *h=(op_hdr*)packet.bytes;h->op=ops[kind];h->size=24;
  CHECK(nvn_handle_pack_one(packet.bytes+8,stage,slots[slot],values[v])==16);
  NvnOpScratch scratch;memset(&scratch,0xcd,sizeof scratch);
  const p_bind_handles *b=nvn_op_normalize(h,&scratch);
  CHECK(b&&b->stage==stage&&b->first==slots[slot]&&b->n==1&&!b->src&&!b->pad_&&b->h[0]==values[v]);
  h->size=16;CHECK(!nvn_op_normalize(h,&scratch));h->size=24;
  uint32_t bad=stage;memcpy(packet.bytes+8,&bad,4);CHECK(!nvn_op_normalize(h,&scratch));
 }
 CHECK(!nvn_handle_pack_one(NULL,-1,0,0));CHECK(!nvn_handle_pack_one(NULL,6,0,0));
 // Independent single-handle wire layout: stage 4, slot 127, full-width value.
 Packet packet={0};op_hdr *h=(op_hdr*)packet.bytes;h->op=OP_BIND_IMG;h->size=24;
 const uint8_t wire[]={4,0,0,128,127,0,0,0,0xef,0xcd,0xab,0x89,0x67,0x45,0x23,0x01};
 memcpy(packet.bytes+8,wire,sizeof wire);NvnOpScratch scratch;
 const p_bind_handles *b=nvn_op_normalize(h,&scratch);
 CHECK(b&&b->stage==4&&b->first==127&&b->n==1&&!b->src&&b->h[0]==UINT64_C(0x0123456789abcdef));
 uint32_t bad=NVN_RECORD_PACKED|6;memcpy(packet.bytes+8,&bad,4);CHECK(!nvn_op_normalize(h,&scratch));
 // Legacy immediate/deferred arrays retain their source pointer and all handles.
 for(unsigned kind=0;kind<3;kind++)for(int n=0;n<=32;n++)for(unsigned deferred=0;deferred<2;deferred++){
  p_bind_handles expected={.stage=5,.first=7,.n=n,.src=deferred?UINT64_C(0x1122334455667788):0};
  for(int i=0;i<n;i++)expected.h[i]=UINT64_C(0xfedcba9876543210)+(uint64_t)i;
  h->op=ops[kind];h->size=8+24+8*n;memcpy(packet.bytes+8,&expected,24+8*n);
  b=nvn_op_normalize(h,&scratch);CHECK(b==(const void*)(packet.bytes+8)&&!memcmp(b,&expected,24+8*n));
  h->size-=8;CHECK(!nvn_op_normalize(h,&scratch));
 }
}
static void buffer_cases(void){
 const unsigned ops[]={OP_BIND_UBO,OP_BIND_SSBO};
 const int stages[]={-1,0,4,5,6,INT32_MAX},slots[]={-1,0,31,255,256,INT32_MAX};
 const uint64_t sizes[]={0,1,65536,UINT32_MAX,UINT64_C(0x100000000),UINT64_MAX};
 const uint64_t addresses[]={0,1,UINT64_C(0x7000123456),UINT64_MAX};
 for(unsigned k=0;k<2;k++)for(unsigned s=0;s<6;s++)for(unsigned i=0;i<6;i++)for(unsigned z=0;z<6;z++)for(unsigned a=0;a<4;a++){
  p_bind_buf expected={stages[s],slots[i],addresses[a],sizes[z]};Packet packet={0};
  op_hdr *h=(op_hdr*)packet.bytes;h->op=ops[k];size_t n=nvn_buffer_pack(NULL,&expected);
  CHECK(n==16||n==24);CHECK(nvn_buffer_pack(packet.bytes+8,&expected)==n);h->size=8+(uint32_t)n;
  NvnOpScratch scratch;memset(&scratch,0xcd,sizeof scratch);const p_bind_buf *r=nvn_op_normalize(h,&scratch);
  CHECK(r&&!memcmp(r,&expected,sizeof expected));
  h->size=16;CHECK(!nvn_op_normalize(h,&scratch));
  // Legacy wire records remain accepted even when compact encoding is possible.
  h->size=32;memcpy(packet.bytes+8,&expected,sizeof expected);
  r=nvn_op_normalize(h,&scratch);CHECK(r==(const void*)(packet.bytes+8)&&!memcmp(r,&expected,sizeof expected));
 }
 Packet packet={0};op_hdr *h=(op_hdr*)packet.bytes;h->op=OP_BIND_SSBO;h->size=24;
 const uint8_t wire[]={0xfd,0x07,0,0x80,0xff,0xff,0xff,0xff,0xef,0xcd,0xab,0x89,0x67,0x45,0x23,0x01};
 memcpy(packet.bytes+8,wire,sizeof wire);NvnOpScratch scratch;
 const p_bind_buf *r=nvn_op_normalize(h,&scratch);
 CHECK(r&&r->stage==5&&r->index==255&&r->size==UINT32_MAX&&r->addr==UINT64_C(0x0123456789abcdef));
 const uint32_t bad[]={0,5,NVN_RECORD_PACKED|6,NVN_RECORD_PACKED|0x800,NVN_RECORD_PACKED|0x1000000};
 for(unsigned i=0;i<sizeof bad/sizeof bad[0];i++){memcpy(packet.bytes+8,&bad[i],4);CHECK(!nvn_op_normalize(h,&scratch));}
 for(unsigned first=0;first<16;first++)for(unsigned a=0;a<4;a++)for(unsigned z=0;z<6;z++){
  NVNbufferRange expected={addresses[a],sizes[z]};memset(&packet,0,sizeof packet);h->op=OP_BIND_VBUF;
  size_t n=nvn_array_pack(packet.bytes+8,(int)first,1,&expected,sizeof expected);CHECK(n==16||n==24);h->size=8+(uint32_t)n;
  const p_bind_vbuf *v=nvn_op_normalize(h,&scratch);CHECK(v&&v->first==(int)first&&v->n==1&&!memcmp(&v->r[0],&expected,sizeof expected));
 }
 // Independent short vertex-range wire; its second word is a size, not count.
 memset(&packet,0,sizeof packet);h->op=OP_BIND_VBUF;h->size=24;memcpy(packet.bytes+8,wire,sizeof wire);
 uint32_t vertex_flags=NVN_RECORD_PACKED|15;memcpy(packet.bytes+8,&vertex_flags,4);
 const p_bind_vbuf *v=nvn_op_normalize(h,&scratch);
 CHECK(v&&v->first==15&&v->n==1&&v->r[0].size==UINT32_MAX&&v->r[0].address==UINT64_C(0x0123456789abcdef));
 vertex_flags=NVN_RECORD_PACKED|16;memcpy(packet.bytes+8,&vertex_flags,4);CHECK(!nvn_op_normalize(h,&scratch));
}
static void indexed_cases(void){
 const int prims[]={-1,0,4,255,256},types[]={-1,0,1,2,256};
 const int bases[]={0,1,-1,INT32_MIN,INT32_MAX},instances[]={0,1,2,INT32_MAX},counts[]={-1,0,3,INT32_MAX};
 const int starts[]={0,5,INT32_MIN};const uint64_t addresses[]={0,UINT64_MAX-7,UINT64_C(0x7000123456)};
 for(unsigned p=0;p<5;p++)for(unsigned t=0;t<5;t++)for(unsigned b=0;b<5;b++)for(unsigned i=0;i<4;i++)for(unsigned c=0;c<4;c++)for(unsigned s=0;s<3;s++)for(unsigned a=0;a<3;a++){
  p_draw_idx expected={prims[p],types[t],counts[c],bases[b],starts[s],instances[i],addresses[a]};
  Packet packet={0};op_hdr *h=(op_hdr*)packet.bytes;h->op=OP_DRAW_IDX;
  size_t n=nvn_indexed_pack(NULL,&expected);CHECK(n==16||n==20||n==24||n==32);
  CHECK(nvn_indexed_pack(packet.bytes+8,&expected)==n);h->size=8+aligned(n);NvnOpScratch scratch;
  const p_draw_idx *r=nvn_op_normalize(h,&scratch);CHECK(r&&!memcmp(r,&expected,sizeof expected));
  h->size=8;CHECK(!nvn_op_normalize(h,&scratch));
  h->size=40;memcpy(packet.bytes+8,&expected,sizeof expected);r=nvn_op_normalize(h,&scratch);
  CHECK(r==(const void*)(packet.bytes+8)&&!memcmp(r,&expected,sizeof expected));
 }
 Packet packet={0};op_hdr *h=(op_hdr*)packet.bytes;h->op=OP_DRAW_IDX;h->size=32;NvnOpScratch scratch;
 const uint8_t wire[]={4,1,5,128,3,0,0,0,0xef,0xcd,0xab,0x89,0x67,0x45,0x23,0x01,0xf9,0xff,0xff,0xff,0,0,0,0};
 memcpy(packet.bytes+8,wire,sizeof wire);const p_draw_idx *r=nvn_op_normalize(h,&scratch);
 CHECK(r&&r->prim==4&&r->itype==1&&r->count==3&&r->base_vertex==-7&&!r->base_instance&&!r->instances&&r->ibuf==UINT64_C(0x0123456789abcdef));
 h->size=24;CHECK(!nvn_op_normalize(h,&scratch));h->size=32;
 uint32_t bad=NVN_RECORD_PACKED|(7u<<16);memcpy(packet.bytes+8,&bad,4);CHECK(!nvn_op_normalize(h,&scratch));
 bad=NVN_RECORD_PACKED|(1u<<20);memcpy(packet.bytes+8,&bad,4);CHECK(!nvn_op_normalize(h,&scratch));
 bad=4;memcpy(packet.bytes+8,&bad,4);CHECK(!nvn_op_normalize(h,&scratch));
}
int main(void){rt_cases();array_cases();handle_cases();buffer_cases();indexed_cases();independent_wire_cases();printf("packed records: %u semantic/bounds checks passed\n",checks);return 0;}
'''

@unittest.skipUnless(os.name=='nt' and CC.is_file(),'Windows Clang required')
class PackedRecordTests(unittest.TestCase):
    def test_semantics_legacy_and_malformed_records(self):
        with tempfile.TemporaryDirectory(prefix='hwaoi-packed-records-') as temp:
            folder=Path(temp);src=folder/'fixture.c';src.write_text(CODE,encoding='utf-8')
            for opt in ('-O0','-O2'):
                exe=folder/'fixture.exe'
                built=subprocess.run([str(CC),opt,'-Wall','-Wextra','-Werror','-I',str(ROOT/'runtime'),str(src),'-o',str(exe)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
                self.assertEqual(built.returncode,0,built.stdout+built.stderr)
                result=subprocess.run([str(exe)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
