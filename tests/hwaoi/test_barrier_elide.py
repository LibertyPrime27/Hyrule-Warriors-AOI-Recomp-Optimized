"""Which guest nvnCommandBufferBarrier masks still record a Vulkan barrier (runtime/vk_exec.c)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from nvn2_compute_c0_test import function
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")


def source():
    text = (ROOT / "runtime/vk_exec.c").read_text(encoding="utf-8")
    return r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define CHECK(x) do{if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
static const char *setting;
const char *cfg_get(const char *key){CHECK(!strcmp(key,"TOTK_BARRIER_ELIDE"));return setting;}
''' + function(text, "int vk_exec_barrier_needed(") + r'''
int main(int argc,char **argv){
 CHECK(argc==2);setting=!strcmp(argv[1],"off")?"0":!strcmp(argv[1],"on")?"1":NULL;int elide=strcmp(argv[1],"off")!=0;
 for(int bits=0;bits<256;bits++){
  int order=(bits&0xf)!=0;   // ORDER_PRIMITIVES/FRAGMENTS/FRAGMENTS_TILED/INDIRECT_DATA wait for earlier work
  CHECK(vk_exec_barrier_needed(bits)==(elide?order:1));
 }
 CHECK(vk_exec_barrier_needed(0x100));CHECK(vk_exec_barrier_needed(0x40000010));   // unknown bits: always recorded
 // The masks a fight issues: texture invalidation alone and none are dropped, ordering is kept.
 CHECK(vk_exec_barrier_needed(0x10)==!elide && vk_exec_barrier_needed(0)==!elide);
 CHECK(vk_exec_barrier_needed(0x21) && vk_exec_barrier_needed(0x12) && vk_exec_barrier_needed(0x3) && vk_exec_barrier_needed(0x8));
 puts("barrier elision checks passed");return 0;
}
'''


@unittest.skipUnless(CC.is_file(), "test Clang not installed")
class BarrierElideTests(unittest.TestCase):
    def test_only_order_bits_record_a_barrier_and_the_switch_restores_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "elide.c"
            path.write_text(source(), encoding="utf-8")
            exe = path.with_suffix(".exe")
            subprocess.run([str(CC), "-O2", "-Wall", "-Wextra", "-Werror", str(path), "-o", str(exe)], check=True)
            for case in ("default", "on", "off"):
                result = subprocess.run([str(exe), case], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, case + ": " + result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
