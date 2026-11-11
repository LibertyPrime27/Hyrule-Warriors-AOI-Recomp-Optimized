"""Original surface shaders and independent SPIR-V validation; no captured game bytes."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from xlate_smoke import EXIT, SCHED, fragment_code, mov32i

EXE = Path(os.environ.get("HWAOI_TEST_XLATE", str(ROOT / "work/build/hwaoi196608/xlate-managed-irbanks/totk_xlate.exe")))
VAL = Path("F:/GCC/ucrt64/bin/spirv-val.exe")
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
DEPS = Path("F:/TotKDependencies/runtime/deps/include")


def instructions(data):
    words = struct.unpack(f"<{len(data) // 4}I", data)
    assert words[0] == 0x07230203
    offset = 5
    while offset < len(words):
        count, opcode = words[offset] >> 16, words[offset] & 65535
        assert count and offset + count <= len(words)
        yield opcode, words[offset + 1:offset + count]
        offset += count


def surface(store, typed):
    # Original Maxwell SULD/SUST(.D), 2D coordinates R4/R5, data R0..R3,
    # image handle c[2][0x40] (TidB is in words), predicate PT.
    prefix = (0xEB38000000000000 if typed else 0xEB28000000000000) if store else (
        0xEB18000000000000 if typed else 0xEB08000000000000)
    return prefix | (16 << 36) | (3 << 33) | (7 << 30) | ((6 if typed else 15) << 20) | (7 << 16) | (4 << 8)


def fixture(read=False, write=False, typed=False):
    ops = [mov32i(i, value) for i, value in enumerate((1.0, 0.5, 0.25, 1.0, 0.0, 0.0))]
    if read:
        ops.append(surface(False, typed))
    if write:
        ops.append(surface(True, typed))
    ops.append(EXIT)
    words = []
    for i in range(0, len(ops), 3):
        batch = ops[i:i + 3]
        words += [SCHED] + batch + [0] * (3 - len(batch))
    return fragment_code()[:80] + struct.pack(f"<{len(words)}Q", *words)


def validate(path):
    # Use an ASCII argv filename: this Windows SPIRV-Tools build crashes on an
    # absolute Chinese path, although the very same bytes validate via cwd.
    return subprocess.run([str(VAL), "--target-env", "vulkan1.3", path.name], cwd=path.parent,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)


@unittest.skipUnless(os.name == "nt" and EXE.is_file() and VAL.is_file(), "local translator and SPIR-V validator required")
class StorageImageTranslatorTests(unittest.TestCase):
    def test_read_write_typed_and_unused_capabilities(self):
        with tempfile.TemporaryDirectory(prefix="image-capability-", dir=ROOT / "work/build") as tmp:
            folder = Path(tmp)
            requests, cases = [], []
            for typed in (False, True):
                for read, write in ((False, False), (True, False), (False, True), (True, True)):
                    name = f"typed{int(typed)}_read{int(read)}_write{int(write)}"
                    code = folder / f"{name}.bin"
                    code.write_bytes(fixture(read, write, typed))
                    job = dict(key=name, stage="ps", code=str(code), debug=False, fout={"0": "float"})
                    requests.append(json.dumps(dict(job=job, out=str(folder))))
                    cases.append((name, typed, read, write))
            result = subprocess.run([str(EXE), "--serve"], input="\n".join(requests) + "\n",
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [f"ok {x[0]}" for x in cases])
            for name, typed, read, write in cases:
                with self.subTest(name=name):
                    path = folder / f"{name}.spv"
                    data = path.read_bytes()
                    ops = list(instructions(data))
                    caps = [o[0] for op, o in ops if op == 17]
                    self.assertEqual(caps.count(55), int(read and not typed))
                    self.assertEqual(caps.count(56), int(write and not typed))
                    self.assertEqual(any(op == 98 for op, _ in ops), read)
                    self.assertEqual(any(op == 99 for op, _ in ops), write)
                    # Check the actual image declaration consumed by each read,
                    # independently of the translator's AstTextureOperation.Format.
                    image_types = {o[0]: o[7] for op, o in ops if op == 25}
                    loads = {o[1]: o[0] for op, o in ops if op == 61}
                    for op, o in ops:
                        if op == 98:
                            self.assertEqual(image_types[loads[o[2]]] == 0, not typed)
                    checked = validate(path)
                    self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                    if read and not typed:
                        # The old behavior is a meaningful failing control: remove
                        # only this declaration, preserving every image operation.
                        words = list(struct.unpack("<5I", data[:20]))
                        for op, operands in ops:
                            if (op, operands) != (17, (55,)):
                                words += [((len(operands) + 1) << 16) | op, *operands]
                        mutant = folder / f"{name}-missing-read-cap.spv"
                        mutant.write_bytes(struct.pack(f"<{len(words)}I", *words))
                        checked = validate(mutant)
                        self.assertNotEqual(checked.returncode, 0)
                        self.assertIn("StorageImageReadWithoutFormat", checked.stderr)


def device_fixture():
    source = (ROOT / "runtime/vk_device.c").read_text()
    start = source.index("    const VkPhysicalDeviceFeatures *h = &c->f2.features;")
    end = source.index("    if (!c->fault.deviceFault)", start)
    actual = source[start:end]
    return r'''
#include <vulkan/vulkan.h>
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef struct { VkPhysicalDeviceFeatures2 f2; VkPhysicalDeviceVulkan12Features f12;
 VkPhysicalDeviceVulkan13Features f13; VkPhysicalDeviceDepthClipControlFeaturesEXT clip; } Chain;
static struct { struct { char deviceName[32]; } props; } g_vk;
static char missing[128];
#define rt_log(fmt,device,feature) ((void)(device),snprintf(missing,sizeof missing,"%s",feature))
static int accept(Chain *c) {
''' + actual + r'''
free(c);return 1;
}
static Chain *make(void) {
 Chain *c=calloc(1,sizeof *c);assert(c);
 c->f12.timelineSemaphore=1;c->f13.dynamicRendering=1;c->f13.synchronization2=1;
 c->clip.depthClipControl=1;
 VkPhysicalDeviceFeatures *h=&c->f2.features;
 h->shaderStorageImageReadWithoutFormat=h->shaderStorageImageWriteWithoutFormat=1;
 h->shaderInt64=h->samplerAnisotropy=h->independentBlend=h->geometryShader=h->textureCompressionBC=1;
 h->drawIndirectFirstInstance=1;
 return c;
}
int main(void) {
 assert(accept(make()));
 Chain *c=make();c->f2.features.shaderStorageImageReadWithoutFormat=0;
 assert(!accept(c));assert(!strcmp(missing,"shaderStorageImageReadWithoutFormat"));
 c=make();c->f2.features.shaderStorageImageWriteWithoutFormat=0;
 assert(!accept(c));assert(!strcmp(missing,"shaderStorageImageWriteWithoutFormat"));
 c=make();c->f2.features.shaderStorageImageReadWithoutFormat=0;
 c->f2.features.shaderStorageImageWriteWithoutFormat=0;
 assert(!accept(c));assert(!strcmp(missing,"shaderStorageImageReadWithoutFormat"));
 c=make();c->f2.features.drawIndirectFirstInstance=0;
 assert(!accept(c));assert(!strcmp(missing,"drawIndirectFirstInstance"));
 puts("device unformatted image feature checks passed");return 0;
}
'''


@unittest.skipUnless(os.name == "nt" and CC.is_file() and DEPS.is_dir(), "Windows Clang and Vulkan headers required")
class StorageImageDeviceTests(unittest.TestCase):
    def test_actual_device_rejection_branch(self):
        with tempfile.TemporaryDirectory(prefix="image-feature-") as tmp:
            source = Path(tmp) / "fixture.c"
            source.write_text(device_fixture())
            for opt in ("-O0", "-O2"):
                exe = source.with_name(f"fixture{opt}.exe")
                built = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", "-I", str(DEPS),
                                        str(source), "-o", str(exe)], capture_output=True, text=True, timeout=60)
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                checked = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                self.assertIn("checks passed", checked.stdout)


if __name__ == "__main__":
    unittest.main()
