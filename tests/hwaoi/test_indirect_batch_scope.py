"""Production metadata/attachment guards for optional indirect pass batching."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
CC=Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
DEPS=Path("F:/TotKDependencies/runtime/deps/include")
sys.path.insert(0,str(ROOT/"tests"))
from nvn2_compute_c0_test import function


def source():
    text=(ROOT/"runtime/vk_exec_draw.c").read_text()
    return r'''
#include <stdio.h>
#include <stdlib.h>
#include "vk_exec.h"
#define CHECK(x) do{if(!(x)){fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1);}}while(0)
''' + function(text,"static int pipeline_storage_writes(") + function(text,"static int indirect_can_stay_in_pass(") + r'''
int main(void){
 VkPipeInfo pipeline={0};CHECK(!pipeline_storage_writes(&pipeline));CHECK(pipeline_storage_writes(NULL));
 GfxShaderRes resource={0};VkPipeBinding binding={.kind=GFX_RES_SSBO,.res=&resource};
 pipeline.n_bindings=1;pipeline.bindings=&binding;
 CHECK(!pipeline_storage_writes(&pipeline));resource.flags=GFX_RES_SSBO_GUARDED;
 CHECK(!pipeline_storage_writes(&pipeline));resource.flags|=1;CHECK(pipeline_storage_writes(&pipeline));
 binding.res=NULL;CHECK(pipeline_storage_writes(&pipeline));binding.kind=GFX_RES_IMG;
 CHECK(pipeline_storage_writes(&pipeline));pipeline.bindings=NULL;CHECK(pipeline_storage_writes(&pipeline));
 VkExec ex={0};CHECK(!indirect_can_stay_in_pass(&ex,0x10000,16));ex.in_pass=1;
 CHECK(indirect_can_stay_in_pass(&ex,0x10000,16));ex.pass_storage_writes=1;
 CHECK(!indirect_can_stay_in_pass(&ex,0x10000,16));ex.pass_storage_writes=0;
 CHECK(!indirect_can_stay_in_pass(&ex,0,16));CHECK(!indirect_can_stay_in_pass(&ex,0x10000,0));
 CHECK(!indirect_can_stay_in_pass(&ex,UINT64_MAX-7,16));
 tex_t texture={0};texture.L.size=4096;
 VkTexImage image={.addr=0x20000};VkExecTarget target={.image=(VkImage)(uintptr_t)1,.img=&image,.tex=&texture};
 ex.color[0]=target;CHECK(indirect_can_stay_in_pass(&ex,0x10000,16));
 image.addr=0x10000;CHECK(!indirect_can_stay_in_pass(&ex,0x10000,16));
 image.addr=0x10010;CHECK(indirect_can_stay_in_pass(&ex,0x10000,16));
 image.addr=0xf001;CHECK(!indirect_can_stay_in_pass(&ex,0x10000,16));
 image.addr=0x20000;image.bound=1;CHECK(!indirect_can_stay_in_pass(&ex,0x10000,16));image.bound=0;
 ex.color[0].img=NULL;CHECK(!indirect_can_stay_in_pass(&ex,0x10000,16));ex.color[0]=target;
 texture.L.size=0;CHECK(!indirect_can_stay_in_pass(&ex,0x10000,16));texture.L.size=4096;
 image.addr=UINT64_MAX-1024;CHECK(!indirect_can_stay_in_pass(&ex,0x10000,16));
 ex.color[0]=(VkExecTarget){0};image.addr=0x10000;ex.depth=target;
 CHECK(!indirect_can_stay_in_pass(&ex,0x10000,16));
 puts("indirect batch scope passed");return 0;
}
'''


@unittest.skipUnless(os.name=="nt" and CC.is_file() and DEPS.is_dir(),"local Clang/Vulkan headers required")
class IndirectBatchScope(unittest.TestCase):
    def test_storage_writers_and_attachment_aliases_retain_barriers(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-indirect-scope-") as folder:
            src=Path(folder)/"fixture.c";exe=src.with_suffix(".exe")
            src.write_text(source(),encoding="utf-8")
            for opt in ("-O0","-O2"):
                build=subprocess.run([str(CC),opt,"-Wall","-Wextra","-Werror","-I",str(ROOT/"runtime"),
                    "-I",str(DEPS),"-I",str(DEPS/"vma"),str(src),"-o",str(exe)],capture_output=True,text=True,timeout=30)
                self.assertEqual(build.returncode,0,build.stdout+build.stderr)
                run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=10)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)


if __name__=="__main__":unittest.main()
