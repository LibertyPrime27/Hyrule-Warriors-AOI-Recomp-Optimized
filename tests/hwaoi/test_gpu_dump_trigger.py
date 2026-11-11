"""Actual trigger parser and one-shot arming; no game, GPU or input automation."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function


def source():
    text = (ROOT / "runtime/vk_dump.c").read_text(encoding="utf-8")
    return r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#define CHECK(x) do {if(!(x)){fprintf(stderr,"check %d: %s\n",__LINE__,#x);exit(1);}}while(0)
static int vk_dump_polling,vk_dump_armed,logs;
static uint64_t dump_frame,trigger_sequence,trigger_next_ns,clock_ns;
static const char *trigger_file;
static char dir[64]="synthetic-dump";
uint64_t kern_now_ns(void){return clock_ns;}
void rt_log(const char *fmt,...){(void)fmt;logs++;}
''' + function(text, "static int dump_trigger_read(") + function(text, "static void dump_trigger_poll(") + r'''
static void contents(const void *data,size_t n){FILE*f=fopen(trigger_file,"wb");CHECK(f);CHECK(fwrite(data,1,n,f)==n);CHECK(!fclose(f));}
static void token(const char*s){contents(s,strlen(s));}
int main(int argc,char **argv){
 CHECK(argc==2);trigger_file=argv[1];uint64_t value=123;
 const char *invalid[]={"","-1","+1","word","1 trailing","1\n2","18446744073709551616"};
 for(unsigned i=0;i<sizeof invalid/sizeof *invalid;i++){
  token(invalid[i]);CHECK(!dump_trigger_read(trigger_file,&value)&&value==123);
 }
 contents("1\0ignored",9);CHECK(!dump_trigger_read(trigger_file,&value)&&value==123);
 char large[80];memset(large,'1',sizeof large);contents(large,sizeof large);
 CHECK(!dump_trigger_read(trigger_file,&value)&&value==123);
 token(" \r\n42\t");CHECK(dump_trigger_read(trigger_file,&value)&&value==42);
 token("18446744073709551615\n");CHECK(dump_trigger_read(trigger_file,&value)&&value==UINT64_MAX);
 CHECK(!dump_trigger_read("absent-synthetic-trigger",&value)&&value==UINT64_MAX);
 // Startup consumes the current token as baseline. Existing, lower and invalid commands do not arm a frame.
 token("5\n");CHECK(dump_trigger_read(trigger_file,&trigger_sequence));vk_dump_polling=1;
 dump_trigger_poll(10);CHECK(!vk_dump_armed&&vk_dump_polling&&!logs);
 clock_ns+=250000000;token("4");dump_trigger_poll(11);CHECK(!vk_dump_armed);
 clock_ns+=250000000;token("invalid");dump_trigger_poll(12);CHECK(!vk_dump_armed);
 // Reads are throttled; the next complete frame is selected, never the already finished present.
 token("6");clock_ns+=249999999;dump_trigger_poll(13);CHECK(!vk_dump_armed);
 clock_ns++;dump_trigger_poll(14);CHECK(vk_dump_armed&&!vk_dump_polling&&dump_frame==15&&trigger_sequence==6&&logs==1);
 token("7");clock_ns+=1000000000;dump_trigger_poll(16);CHECK(dump_frame==15&&logs==1);
 // A wrapping target frame cannot be armed, even with a valid fresh token.
 vk_dump_polling=1;vk_dump_armed=0;trigger_next_ns=0;dump_trigger_poll(UINT64_MAX);
 CHECK(!vk_dump_armed&&vk_dump_polling&&dump_frame==15);
 puts("GPU dump trigger checks passed");return 0;
}
'''


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Windows Clang required")
class GpuDumpTriggerTests(unittest.TestCase):
    def test_strict_tokens_baseline_throttle_and_single_complete_frame(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-dump-trigger-") as folder:
            path = Path(folder) / "fixture.c"
            path.write_text(source(), encoding="utf-8")
            for opt in ("-O0", "-O2"):
                exe = path.with_suffix(".exe")
                built = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", str(path), "-o", str(exe)],
                                       capture_output=True, text=True)
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                run = subprocess.run([str(exe), str(Path(folder) / "trigger.txt")], cwd=folder,
                                     capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
