"""Expiring live diagnostic controls: original values and malformed/stale updates."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
CC=Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
sys.path.insert(0,str(ROOT/"tests"))
from nvn2_compute_c0_test import function

SOURCE=r'''
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include "input_feed.h"
int main(void) {
 InputFeed f={0};uint64_t buttons=2;int32_t l[2]={100,0},r[2]={0,-200};
 assert(input_feed_accept(&f,"1 8 0 32767 -32767 0 100",1000));
 input_feed_merge(&f,1001,&buttons,l,r);assert(buttons==10 && l[0]==100 && l[1]==32767 && r[0]==-32767 && r[1]==-200);
 uint64_t end=f.until;InputFeed before=f;
 const char *bad[]={"1 ffff 0 0 0 0 9999","0 0 0 0 0 0 1","-2 0 0 0 0 0 1",
 "18446744073709551616 0 0 0 0 0 1","2 10000 0 0 0 0 1","2 -1 0 0 0 0 1",
 "2 1 -32768 0 0 0 1","2 1 32768 0 0 0 1","2 1 0 0 0 0 -1","2 1 0 0 0 0 10001",
 "2 1 0 0 0 0 1 garbage","2 1 0 0 0 0","2 1 0 0 0 0 18446744073709551616"};
 for(unsigned i=0;i<sizeof bad/sizeof *bad;i++) {
  assert(!input_feed_accept(&f,bad[i],end-1));assert(!memcmp(&f,&before,sizeof f));
 }
 buttons=0;l[0]=l[1]=r[0]=r[1]=0;input_feed_merge(&f,end,&buttons,l,r);assert(!buttons && !l[1] && !r[0]);
 assert(input_feed_accept(&f,"2 0 0 0 0 0 0",end));input_feed_merge(&f,end,&buttons,l,r);assert(!buttons);
 assert(input_feed_accept(&f,"3 ffff 1 2 3 4 10000\n",end));
 before=f;assert(!input_feed_accept(&f,"4 0 0 0 0 0 1",UINT64_MAX-100));assert(!memcmp(&f,&before,sizeof f));
 assert(input_feed_accept(&f,"4 0 0 0 0 0 0 90 3840 2160",end));assert(f.fps==90 && f.width==3840 && f.height==2160);
 before=f;assert(!input_feed_accept(&f,"5 0 0 0 0 0 0 75 1920 1080",end));assert(!memcmp(&f,&before,sizeof f));
 assert(!input_feed_accept(&f,"5 0 0 0 0 0 0 60 3841 2160",end));assert(!memcmp(&f,&before,sizeof f));
 assert(!input_feed_accept(&f,"5 0 0 0 0 0 0 120 1920 0",end));assert(!memcmp(&f,&before,sizeof f));
 puts("input feed checks passed");return 0;
}
'''


@unittest.skipUnless(os.name=="nt" and CC.is_file(),"Windows Clang required")
class InputFeedTests(unittest.TestCase):
    def test_actual_file_polling_baseline_expiry_and_bad_updates(self):
        body=function((ROOT/"runtime/input.c").read_text(encoding="utf-8"),"static void feed_apply(")
        fixture=r'''
#include <assert.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "input_feed.h"
static uint64_t now=1;
static const char *feed_path;
static int logs;
static int config_calls;
static int frame_step_native_rate(void){return 0;}
static uint64_t plat_now_ns(void){return now;}
static const char *cfg_get(const char *key){
 if(!strcmp(key,"TOTK_EXPERIMENTAL_RATE"))return "1";
 assert(!strcmp(key,"TOTK_INPUT_FEED"));return feed_path;
}
static int cfg_from_env(const char *key){(void)key;return 0;}
static void cfg_set(const char *key,const char *value){
 if(!strcmp(key,"TOTK_FPS_LIMIT"))assert(!strcmp(value,"90"));
 else {assert(!strcmp(key,"TOTK_WINDOW_SIZE"));assert(!strcmp(value,"3840x2160"));}
 config_calls++;
}
static void rt_log(const char *fmt,...){(void)fmt;logs++;}
''' + body + r'''
static void write_line(const char *line){FILE *f=fopen(feed_path,"w");assert(f);fputs(line,f);fclose(f);}
static void check(uint64_t wanted,int y){uint64_t b=0;int32_t l[2]={0},r[2]={0};feed_apply(&b,l,r);assert(b==wanted && l[1]==y);}
int main(void){
 feed_path="feed.txt";
 write_line("1 8 0 32767 0 0 1000\n");check(0,0);assert(logs==2); // launch baseline cannot replay
 now+=40000000;write_line("2 8 0 32767 0 0 1000\n");check(8,32767);
 now+=40000000;check(8,32767); // same sequence keeps its original deadline
 now+=1100000000;check(0,0);
 now+=40000000;write_line("3 8 0 32767 0 0 1000\nextra\n");check(0,0);
 now+=40000000;write_line("3 8 0 32767 0 0 1000\n");check(8,32767);
 remove(feed_path);now+=40000000;check(8,32767);
 now+=1100000000;check(0,0);
 write_line("4 0 0 0 0 0 0 90 3840 2160\n");now+=40000000;check(0,0);assert(config_calls==2);
 remove(feed_path);puts("file polling checks passed");return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="hwaoi-feed-poll-") as temp:
            path=Path(temp)/"fixture.c";path.write_text(fixture,encoding="utf-8")
            for opt in ("-O0","-O2"):
                exe=Path(temp)/"fixture.exe"
                build=subprocess.run([str(CC),opt,"-Wall","-Wextra","-Werror","-I",str(ROOT/"runtime"),
                    str(path),"-o",str(exe)],capture_output=True,text=True)
                self.assertEqual(build.returncode,0,build.stderr)
                run=subprocess.run([str(exe)],cwd=temp,capture_output=True,text=True,timeout=5)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)

    def test_expiry_bounds_and_merge(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-input-feed-") as temp:
            p=Path(temp)/"test.c";p.write_text(SOURCE,encoding="utf-8")
            for opt in ("-O0","-O2"):
                exe=Path(temp)/"test.exe"
                build=subprocess.run([str(CC),opt,"-Wall","-Wextra","-Werror","-I",str(ROOT/"runtime"),
                    str(p),"-o",str(exe)],capture_output=True,text=True)
                self.assertEqual(build.returncode,0,build.stderr)
                run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=5)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)


if __name__=="__main__":unittest.main()
