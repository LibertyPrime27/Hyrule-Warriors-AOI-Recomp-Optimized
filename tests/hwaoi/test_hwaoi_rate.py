"""Original rate-period cases; gameplay speed remains a separate runtime check."""
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


@unittest.skipUnless(os.name=="nt" and CC.is_file(),"Windows Clang required")
class HwaoiRateTests(unittest.TestCase):
    def test_native_step_profile_guard_and_live_rate_word(self):
        source = r'''
#include <assert.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include "frame_rate.c"
const int rt_game_version=0;
uint64_t rt_guest_start_ns;unsigned rt_trace;
static uint8_t page[4096];static unsigned version=1;static const char *rate="60";
static int has_step=1,rate_on=1;
const char *cfg_get(const char *key){
 if(!strcmp(key,"TOTK_RATE_UNLOCK"))return "1";
 if(!strcmp(key,"TOTK_FPS_LIMIT"))return rate;
 return NULL;
}
unsigned cfg_version(void){return version;}
int rt_patch_set(const char *name){return !strcmp(name,"hwaoi_rate")?0:!strcmp(name,"hwaoi_step")&&has_step?1:-1;}
int rt_patch_enabled(int set){return set==0?rate_on:set==1;}
void *rt_patch_page(void){return page;}
void rt_patch_disable(int set){(void)set;assert(0);}
void rt_patch_switch(int set,int on){(void)set;(void)on;assert(0);}
double gfx_window_refresh_hz(void){return 60;}
uint64_t kern_now_ns(void){return 0;}
int plat_mem_read(uint64_t a,void *out,size_t n){(void)a;(void)out;(void)n;assert(0);return 0;}
void rt_log(const char *fmt,...){(void)fmt;}
void rt_fatal(const char *fmt,...){(void)fmt;exit(73);}
int main(int argc,char **argv){
 assert(argc==2);if(!strcmp(argv[1],"stale"))has_step=0;
 if(!strcmp(argv[1],"step_only"))rate_on=0;
 memset(page,0xa5,sizeof page);frame_step_init();assert(has_step&&rate_on);
 assert(!frame_step_realtime()&&frame_step_native_rate()); // node clock, no borrowed host/TotK clock
 uint64_t now=1000000000;frame_step_present(now,1);
 const char *rates[]={"30","60","90","120","60"};
 const unsigned hz[]={30,60,90,120,60};
 for(unsigned r=0;r<5;r++){
  rate=rates[r];version++;frame_step_cap_ns();
  assert(*(uint32_t*)(page+0xb0)==hz[r]);
  frame_step_present(now,1);
  for(unsigned i=0;i<8;i++){now+=1000000000/hz[r];frame_step_present(now,1);}
 }
 // Presents and loading gaps never write a guessed step into another game's
 // patch words. Only the native scheduler's target word is host-controlled.
 now+=1000000000;frame_step_present(now,1);
 for(unsigned i=0;i<sizeof page;i++)if(i<0xb0||i>=0xb4)assert(page[i]==0xa5);
 puts("native step guard and live rate word passed");return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="hwaoi-measured-step-") as tmp:
            folder=Path(tmp); path=folder/"fixture.c";path.write_text(source,encoding="utf-8")
            (folder/"layout.h").write_text("#define MOD_MAIN_BASE 0x7000000000ull\n",encoding="ascii")
            for opt in ("-O0","-O2"):
                exe=folder/"fixture.exe"
                result=subprocess.run([str(CC),opt,"-march=x86-64-v3","-I",str(ROOT/"runtime"),"-I",str(folder),str(path),"-o",str(exe)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                for mode,code in (("good",0),("stale",73),("step_only",73)):
                    result=subprocess.run([str(exe),mode],capture_output=True,text=True,timeout=10)
                    self.assertEqual(result.returncode,code,result.stdout+result.stderr)

    def test_actual_present_policy_and_default_behavior(self):
        policy=function((ROOT/"runtime/nvn_api_obj.c").read_text(encoding="utf-8"),"static uint64_t present_period(")
        source=r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include "hwaoi_rate.h"
#define RT_TARGET_HWAOI 1
#define VSYNC_HZ 60ull
typedef struct {int interval;} win_t;
static int experiment,realtime,native_rate;static const char *value;
static const char *cfg_get(const char *key){
 if(!strcmp(key,"TOTK_EXPERIMENTAL_RATE"))return experiment ? "1" : NULL;
 assert(!strcmp(key,"TOTK_FPS_LIMIT"));return value;
}
static const char *cfg_value(const char *key){const char *v=cfg_get(key);return v&&*v?v:"60";}
static int frame_step_realtime(void){return realtime;}
static int frame_step_native_rate(void){return native_rate;}
static uint64_t frame_step_cap_ns(void){return 1234567;}
static int64_t plat_xchg64(volatile int64_t *p,int64_t v){int64_t old=*p;*p=v;return old;}
static void rt_fatal(const char *fmt,...){(void)fmt;exit(73);}
static void rt_log(const char *fmt,...){(void)fmt;}
''' + policy + r'''
int main(int argc,char **argv){
 assert(argc==2);win_t window={2};
 if(!strcmp(argv[1],"bad")){experiment=1;value="144";(void)present_period(&window);return 1;}
 for(int i=-1;i<=4;i++){window.interval=i;uint64_t game=(uint64_t)(i>0?i:1)*1000000000/60;
  assert(present_period(&window)==(game>16666667?game:16666667));}
 realtime=1;window.interval=1;assert(present_period(&window)==16666667); // no borrowed TotK step
 value="30";assert(present_period(&window)==33333333);
 value="120";assert(present_period(&window)==16666666); // unqualified high rates stay bounded
 experiment=1;
 const char *rates[]={"30","60","90","120"};uint64_t periods[]={33333333,16666667,11111111,8333333};
 for(int i=0;i<4;i++){value=rates[i];assert(present_period(&window)==periods[i]);}
 experiment=0;native_rate=1;value="120";assert(present_period(&window)==8333333);
 value=NULL;assert(present_period(&window)==16666667);return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="hwaoi-present-policy-") as temp:
            p=Path(temp)/"fixture.c";p.write_text(source,encoding="utf-8")
            for opt in ("-O0","-O2"):
                exe=Path(temp)/"fixture.exe"
                build=subprocess.run([str(CC),opt,"-Wall","-Wextra","-Werror","-I",str(ROOT/"runtime"),
                    str(p),"-o",str(exe)],capture_output=True,text=True)
                self.assertEqual(build.returncode,0,build.stderr)
                for mode,expected in (("good",0),("bad",73)):
                    run=subprocess.run([str(exe),mode],capture_output=True,text=True,timeout=5)
                    self.assertEqual(run.returncode,expected,run.stdout+run.stderr)

    def test_periods_and_unsupported_modes(self):
        source=r'''
#include <assert.h>
#include <stdio.h>
#include "hwaoi_rate.h"
int main(void){
 uint64_t out=0;
 const char *rates[]={"30","60","90","120"};
 const uint64_t periods[]={33333333,16666667,11111111,8333333};
 for(int i=0;i<4;i++){assert(hwaoi_rate_period(rates[i],&out));assert(out==periods[i]);}
 assert(hwaoi_rate_period(NULL,&out) && out==periods[1]);
 const char *bad[]={"0","-60","75","90fps","120.0","144","display","999999999999999999999999999999"};
 for(unsigned i=0;i<sizeof bad/sizeof *bad;i++){out=42;assert(!hwaoi_rate_period(bad[i],&out));assert(out==42);}
 puts("rate period checks passed");return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="hwaoi-rate-") as temp:
            p=Path(temp)/"fixture.c";p.write_text(source,encoding="utf-8")
            for opt in ("-O0","-O2"):
                exe=Path(temp)/"fixture.exe"
                build=subprocess.run([str(CC),opt,"-Wall","-Wextra","-Werror","-I",str(ROOT/"runtime"),
                    str(p),"-o",str(exe)],capture_output=True,text=True)
                self.assertEqual(build.returncode,0,build.stderr)
                run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=5)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)


if __name__=="__main__":unittest.main()
