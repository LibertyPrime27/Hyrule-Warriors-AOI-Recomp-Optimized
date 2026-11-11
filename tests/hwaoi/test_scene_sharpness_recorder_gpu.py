"""Real Vulkan clone through the production recorder, held to expose ordering.

The consumer is held until the helper returns. A raw vkCmdUpdateBuffer bypass
then runs before queued CopyBuffer and its data is overwritten deterministically.
No game, ROM or active process is used.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
DEPS = Path("F:/TotKDependencies/runtime/deps")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_scene_sharpness_gpu as direct


def source():
    text = direct.source()
    for call in ("CopyBuffer", "PipelineBarrier2", "UpdateBuffer"):
        text = text.replace("#undef vkCmd" + call + "\n", "")
    text = text.replace('#include "vk_exec.h"', '#include <windows.h>\n#include "platform/platform.h"\n#include "vk_exec.h"')
    support = r'''
// Minimal real Windows thread/event providers for the original recorder TU.
// Only its first consumer start is held; no thread or API of a game is touched.
struct PlatThread {HANDLE handle;PlatThreadFn fn;void *argument;};
static HANDLE consumer_gate;
unsigned rt_trace;
int rt_knob(const char *name,const char *def){(void)name;(void)def;return 1;}
int rt_ab(void){return 1;}
void rt_log(const char *format,...){(void)format;}
void vk_crumb(VkCommandBuffer cb,int kind,uint64_t value){(void)cb;(void)kind;(void)value;}
void plat_thread_priority(int priority){(void)priority;}
int plat_thread_priority_get(void){return 0;}
void plat_yield(void){SwitchToThread();}
uint64_t plat_now_ns(void){return GetTickCount64()*1000000ull;}
void plat_event_init(PlatEvent *e,int manual,int initial){
    memset(e,0,sizeof *e);e->m.p=CreateEvent(NULL,manual,initial,NULL);CHECK(e->m.p);
}
void plat_event_set(PlatEvent *e){CHECK(SetEvent((HANDLE)e->m.p));}
int plat_event_wait_ns(PlatEvent *e,uint64_t ns){
    DWORD result=WaitForSingleObject((HANDLE)e->m.p,(DWORD)((ns+999999)/1000000));
    CHECK(result==WAIT_OBJECT_0 || result==WAIT_TIMEOUT);return result==WAIT_OBJECT_0;
}
static DWORD WINAPI begin_consumer(void *raw){
    PlatThread *t=raw;CHECK(WaitForSingleObject(consumer_gate,10000)==WAIT_OBJECT_0);t->fn(t->argument);return 0;
}
PlatThread *plat_thread_new(PlatThreadFn fn,void *arg,const char *name){
    (void)name;CHECK(!consumer_gate);consumer_gate=CreateEvent(NULL,TRUE,FALSE,NULL);CHECK(consumer_gate);
    PlatThread *t=calloc(1,sizeof *t);CHECK(t);t->fn=fn;t->argument=arg;
    t->handle=CreateThread(NULL,0,begin_consumer,t,0,NULL);CHECK(t->handle);return t;
}
'''
    text = text.replace('int main(void){', support + '\nint main(void){')
    text = text.replace('const uint32_t params[]={0x3ecccccd,0x36663666};',
                        'uint32_t params[]={0x3ecccccd,0x36663666};const uint32_t expected_params[]={0x3ecccccd,0x36663666};')
    text = text.replace('  VkDescriptorBufferInfo original={input,OFF,ranges[test]};',
                        '  params[0]=expected_params[0];params[1]=expected_params[1];rec_begin(ex.cb);\n  VkDescriptorBufferInfo original={input,OFF,ranges[test]};')
    text = text.replace('  CHECK(copy.buffer==destination&&copy.offset==OFF&&copy.range==ranges[test]);',
                        '''  CHECK(copy.buffer==destination&&copy.offset==OFF&&copy.range==ranges[test]);
  // The packet must own params after returning, including while consumption is held.
  params[0]=0xdeadbeef;params[1]=0x12345678;
  CHECK(SetEvent(consumer_gate));''')
    text = text.replace('vkCmdPipelineBarrier2(ex.cb,&dep);VK(vkEndCommandBuffer(ex.cb));',
                        'vkCmdPipelineBarrier2(ex.cb,&dep);if(!rec_end_cb(ex.cb,0,0))VK(vkEndCommandBuffer(ex.cb));')
    text = text.replace('((const unsigned char*)params)[k-OFF]',
                        '((const unsigned char*)expected_params)[k-OFF]')
    return text


@unittest.skipUnless(os.name == "nt" and CC.is_file() and DEPS.is_dir(), "local Vulkan toolchain required")
class SceneSharpnessRecorderGpu(unittest.TestCase):
    def test_real_recorder_orders_clone_and_freezes_update_payload(self):
        base = ROOT / "build/test/scene-sharpness-recorder-gpu"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            folder=Path(temporary).resolve()
            self.assertTrue(folder.is_relative_to(base.resolve()))
            path=folder/"fixture.c"
            command=[str(CC),"-O2","-Wall","-Wextra","-Werror","-Wno-missing-field-initializers",
                "-I",str(ROOT/"runtime"),"-I",str(DEPS/"include"),"-I",str(DEPS/"include/vma"),
                str(path),str(ROOT/"runtime/vk_rec.c"),"-L",str(DEPS/"lib"),"-lvulkan-1","-o",str(folder/"fixture.exe")]
            active = source()
            disabled = active.replace('return 1;}\nint rt_ab', 'return 0;}\nint rt_ab')
            disabled = disabled.replace('CHECK(SetEvent(consumer_gate));', '(void)consumer_gate;')
            old = active.replace('#include "vk_exec.h"', '#include "vk_exec.h"\n#undef vkCmdUpdateBuffer')
            for mode, fixture in (("queued", active), ("recorder-off", disabled), ("old-raw-update", old)):
                with self.subTest(mode=mode):
                    path.write_text(fixture,encoding="utf-8")
                    built=subprocess.run(command,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=60)
                    self.assertEqual(built.returncode,0,built.stdout+built.stderr)
                    run=subprocess.run([str(folder/"fixture.exe")],capture_output=True,text=True,timeout=30)
                    if mode == "old-raw-update":
                        # The held consumer makes the old overwrite deterministic;
                        # there is no concurrent recording or induced device loss.
                        self.assertNotEqual(run.returncode,0,run.stdout+run.stderr)
                        self.assertIn("destination_map[k]==result",run.stderr)
                    else:
                        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                        self.assertIn("399360 byte checks",run.stdout)


if __name__=="__main__":
    unittest.main()
