"""Scene preference/profile checks and the real GPU-command hook with inert bytes.

No game payload, ROM or active process is used. Real GPU byte-preservation is
covered separately by test_scene_sharpness_gpu.py.
"""
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
DEPS = Path("F:/TotKDependencies/runtime/deps")
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function


def source():
    draw = (ROOT / "runtime/vk_exec_draw.c").read_text(encoding="utf-8")
    functions = "\n".join(function(draw, name) for name in (
        "static VkDescriptorBufferInfo hwaoi_scene_sharpness_copy(",
        "static int hwaoi_scene_sharpness_override("))
    oracle = [struct.unpack("<H", struct.pack("<e", struct.unpack("<f", struct.pack("<f", n / 100))[0]))[0]
              for n in range(101)]
    return r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include "vk_exec.h"
#include "hwaoi_scene_sharpness.h"
#undef vkCmdCopyBuffer
#undef vkCmdPipelineBarrier2
#undef vkCmdUpdateBuffer
#define NVN_STAGE_CS 5
#define CHECK(x) do{if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
static struct {int volatile_;} RS;
static uint8_t control[0x880],code[14736],src[1024],dst[1024],before[1024];
static unsigned commands,hashes,config_reads,logs,allocations;
static int bad_control,bad_code,alias_destination;
static uint64_t origin=HWAOI_SCENE_RCAS_ORIGIN;
static uint32_t code_length=sizeof code;
static const char *setting;
static VkBuffer srcbuf=(VkBuffer)(uintptr_t)11,dstbuf=(VkBuffer)(uintptr_t)22;
static uint32_t observed_params[2];
void rt_fatal(const char *fmt,...) {
    va_list args;va_start(args,fmt);vfprintf(stderr,fmt,args);va_end(args);exit(73);
}
void rt_log(const char *fmt,...) {CHECK(strstr(fmt,"explicit 3D RCAS strength"));logs++;}
const char *cfg_get(const char *key) {CHECK(!strcmp(key,"TOTK_SCENE_SHARPNESS"));config_reads++;return setting;}
static int bound_stage(const VkExec *ex,int stage,const uint8_t **ctl,const uint8_t **bytes,uint32_t *length) {
    CHECK(ex && stage==5);*ctl=control;*bytes=code;*length=code_length;return 1;
}
uint64_t nvn2_origin(const uint8_t *bytes) {CHECK(bytes==code);return origin;}
void rt_sha256(const void *data,size_t size,uint8_t out[32]) {
    static const uint8_t expected_ctl[32]={0xc0,0x9e,0x90,0xf4,0x9a,0x98,0x4d,0x19,0xc5,0xf9,0x46,0x14,0x80,0x95,0x72,0x46,0xa4,0xd1,0x0b,0xa2,0x71,0x22,0x17,0xee,0xf3,0x6a,0xa2,0xa8,0xce,0x4b,0x23,0xe7};
    static const uint8_t expected_code[32]={0xae,0x2c,0x8d,0x81,0xd7,0x9b,0xb6,0xdc,0xc1,0xdf,0x13,0x70,0xcd,0xd1,0xe3,0x6e,0xd1,0xda,0x60,0x2c,0x35,0x73,0xac,0x9c,0x8f,0xeb,0xf3,0xe7,0x5b,0x1b,0x8e,0xad};
    hashes++;
    if(data==control){CHECK(size==sizeof control);memcpy(out,expected_ctl,32);if(bad_control)out[0]^=1;}
    else {CHECK(data==code && size==sizeof code);memcpy(out,expected_code,32);if(bad_code)out[0]^=1;}
}
static uint8_t *scratch(VkExec *ex,uint64_t size,VkBuffer *buffer,VkDeviceSize *offset) {
    CHECK(ex && size<=256);allocations++;
    *buffer=alias_destination?srcbuf:dstbuf;*offset=alias_destination?128:256;
    return NULL; // a correct command-only clone never reads/writes this CPU pointer
}
VKAPI_ATTR void VKAPI_CALL vkCmdPipelineBarrier2(VkCommandBuffer cb,const VkDependencyInfo *info) {
    CHECK(cb && info->memoryBarrierCount==1 && !info->bufferMemoryBarrierCount && !info->imageMemoryBarrierCount);
    const VkMemoryBarrier2 *b=info->pMemoryBarriers;
    unsigned phase=commands++%5;CHECK(phase==0 || phase==2 || phase==4);
    if(phase==0)CHECK(b->srcStageMask==VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT && b->srcAccessMask==VK_ACCESS_2_MEMORY_WRITE_BIT &&
        b->dstStageMask==VK_PIPELINE_STAGE_2_ALL_TRANSFER_BIT && b->dstAccessMask==VK_ACCESS_2_TRANSFER_READ_BIT);
    if(phase==2)CHECK(b->srcStageMask==VK_PIPELINE_STAGE_2_ALL_TRANSFER_BIT && b->srcAccessMask==VK_ACCESS_2_TRANSFER_WRITE_BIT &&
        b->dstStageMask==VK_PIPELINE_STAGE_2_ALL_TRANSFER_BIT && b->dstAccessMask==VK_ACCESS_2_TRANSFER_WRITE_BIT);
    if(phase==4)CHECK(b->srcStageMask==VK_PIPELINE_STAGE_2_ALL_TRANSFER_BIT && b->srcAccessMask==VK_ACCESS_2_TRANSFER_WRITE_BIT &&
        b->dstStageMask==VK_PIPELINE_STAGE_2_COMPUTE_SHADER_BIT && b->dstAccessMask==VK_ACCESS_2_UNIFORM_READ_BIT);
}
VKAPI_ATTR void VKAPI_CALL vkCmdCopyBuffer(VkCommandBuffer cb,VkBuffer source,VkBuffer target,uint32_t count,const VkBufferCopy *copies) {
    CHECK(cb && commands++%5==1 && source==srcbuf && target==dstbuf && count==1);
    CHECK(copies[0].srcOffset==128 && copies[0].dstOffset==256 && copies[0].size==256);
    memcpy(dst+copies[0].dstOffset,src+copies[0].srcOffset,(size_t)copies[0].size);
}
VKAPI_ATTR void VKAPI_CALL vkCmdUpdateBuffer(VkCommandBuffer cb,VkBuffer target,VkDeviceSize offset,VkDeviceSize size,const void *params) {
    CHECK(cb && commands++%5==3 && target==dstbuf && offset==256 && size==8);
    memcpy(observed_params,params,8);memcpy(dst+offset,params,(size_t)size);
}
''' + functions + '\nstatic const uint16_t half_oracle[101]={' + ','.join(hex(n) for n in oracle) + r'''};
static void policy(void) {
    int p=991;CHECK(hwaoi_scene_sharpness_parse(NULL,&p) && p==-1);
    CHECK(hwaoi_scene_sharpness_parse("game",&p) && p==-1);
    CHECK(!hwaoi_scene_sharpness_parse("1",NULL));
    const char *bad[]={"","Game","-1","101","1.0"," 40","40%","+1","99999999999999999999"};
    for(unsigned i=0;i<sizeof bad/sizeof *bad;i++){p=991;CHECK(!hwaoi_scene_sharpness_parse(bad[i],&p) && p==991);}
    for(int n=0;n<=100;n++) {
        char value[8];snprintf(value,sizeof value,"%d",n);CHECK(hwaoi_scene_sharpness_parse(value,&p) && p==n);
        uint32_t words[2]={0x11223344,0x55667788};CHECK(hwaoi_scene_sharpness_words(n,words));
        CHECK(words[1]==((uint32_t)half_oracle[n]|(uint32_t)half_oracle[n]<<16));
        float expected=(float)n/100.0f;uint32_t bits;memcpy(&bits,&expected,4);CHECK(words[0]==bits);
    }
    uint32_t words[2]={11,22};CHECK(!hwaoi_scene_sharpness_words(-1,words) && words[0]==11 && words[1]==22);
    CHECK(!hwaoi_scene_sharpness_words(101,words) && !hwaoi_scene_sharpness_words(40,NULL));
    CHECK(!hwaoi_scene_sharpness_match(NULL,sizeof control,code,sizeof code));
    CHECK(!hwaoi_scene_sharpness_match(control,sizeof control-1,code,sizeof code));
    CHECK(!hwaoi_scene_sharpness_match(control,sizeof control,code,sizeof code-1) && !hashes);
    memcpy(control+0x850,"HIR1",4);CHECK(!hwaoi_scene_sharpness_match(control,sizeof control,code,sizeof code) && !hashes);
    puts("scene sharpness policy/profile checks passed");
}
int main(int argc,char **argv) {
    CHECK(argc==2);const char *test=argv[1];
    if(!strcmp(test,"policy")){policy();return 0;}
    for(unsigned i=0;i<sizeof src;i++)src[i]=(uint8_t)(i*37+19);memcpy(before,src,sizeof src);memset(dst,0xe1,sizeof dst);
    prog_t program={0};VkExec ex={.prog=&program,.cb=(VkCommandBuffer)(uintptr_t)3,.recorded=7};
    VkDescriptorBufferInfo original={srcbuf,128,256},out={0};setting="40";
    if(!strcmp(test,"game"))setting="game";
    if(!strcmp(test,"invalid-setting"))setting="101";
    if(!strcmp(test,"wrong-origin"))origin++;
    if(!strcmp(test,"wrong-control"))bad_control=1;
    if(!strcmp(test,"wrong-code"))bad_code=1;
    if(!strcmp(test,"wrong-ir"))memcpy(control+0x850,"HIR1",4);
    if(!strcmp(test,"wrong-length"))code_length--;
    if(!strcmp(test,"wrong-stage") || !strcmp(test,"wrong-slot")) {
        CHECK(!hwaoi_scene_sharpness_override(&ex,!strcmp(test,"wrong-stage")?0:5,!strcmp(test,"wrong-slot")?4:3,&original,&out));
        CHECK(!commands && !hashes && !allocations && !config_reads);puts("scene scope rejected");return 0;
    }
    if(!strcmp(test,"bad-range"))original.range=64;
    if(!strcmp(test,"bad-offset"))original.offset++;
    if(!strcmp(test,"bad-size"))original.range--;
    if(!strcmp(test,"in-pass"))ex.in_pass=1;
    if(!strcmp(test,"null-cb"))ex.cb=VK_NULL_HANDLE;
    if(!strcmp(test,"alias"))alias_destination=1;
    int changed=hwaoi_scene_sharpness_override(&ex,5,3,!strcmp(test,"missing")?NULL:&original,&out);
    if(!strcmp(test,"game") || !strcmp(test,"wrong-origin")) {
        CHECK(!changed && !commands && !hashes && !allocations && !RS.volatile_ && ex.recorded==7);
        CHECK(config_reads==1 && !memcmp(src,before,sizeof src));puts("native descriptor preserved");return 0;
    }
    CHECK(changed && commands==5 && hashes==2 && allocations==1 && config_reads==1 && logs==1 && RS.volatile_ && ex.recorded==12);
    CHECK(out.buffer==dstbuf && out.offset==256 && out.range==256);
    CHECK(!memcmp(src,before,sizeof src));
    for(unsigned i=0;i<sizeof dst;i++) {
        unsigned char expected=0xe1;
        if(i>=256 && i<512)expected=i<264?((const unsigned char*)observed_params)[i-256]:src[128+i-256];
        CHECK(dst[i]==expected);
    }
    CHECK(observed_params[0]==0x3ecccccd && observed_params[1]==0x36663666);
    CHECK(!memcmp(dst+256+64,src+128+64,4)); // preserve the independent integer bypass flag
    CHECK(hwaoi_scene_sharpness_override(&ex,5,3,&original,&out));
    CHECK(hashes==2 && config_reads==1 && commands==10); // immutable verified lowerer buffers are cached
    puts("scene sharpness hook/barrier/tail checks passed");return 0;
}
'''


@unittest.skipUnless(os.name == "nt" and CC.is_file() and DEPS.is_dir(), "existing Windows toolchain required")
class SceneSharpnessHook(unittest.TestCase):
    def test_policy_profile_scope_and_gpu_copy_commands(self):
        base = ROOT / "build/test/scene-sharpness-hook"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            folder=Path(temporary).resolve()
            self.assertTrue(folder.is_relative_to(base.resolve()))
            path,exe=folder/"fixture.c",folder/"fixture.exe"
            path.write_text(source(),encoding="utf-8")
            built=subprocess.run([str(CC),"-O2","-Wall","-Wextra","-Werror","-I",str(ROOT/"runtime"),
                "-I",str(DEPS/"include"),"-I",str(DEPS/"include/vma"),str(path),"-o",str(exe)],
                capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=60)
            self.assertEqual(built.returncode,0,built.stdout+built.stderr)
            success=("policy","normal","game","wrong-origin","wrong-stage","wrong-slot")
            failures=("invalid-setting","wrong-control","wrong-code","wrong-ir","wrong-length","missing",
                      "bad-range","bad-offset","bad-size","in-pass","null-cb","alias")
            for case in success+failures:
                with self.subTest(case=case):
                    run=subprocess.run([str(exe),case],capture_output=True,text=True,timeout=10)
                    self.assertEqual(run.returncode,0 if case in success else 73,run.stdout+run.stderr)


if __name__=="__main__":
    unittest.main()
