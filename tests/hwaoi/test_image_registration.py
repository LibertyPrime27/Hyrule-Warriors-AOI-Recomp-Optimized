"""RegisterImage ABI and real descriptor-copy semantics, using synthetic objects only."""
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
    obj = (ROOT / "runtime/nvn_api_obj.c").read_text()
    vk = (ROOT / "runtime/vk_texture.c").read_text()
    types = vk[vk.index("typedef struct {\n    struct Tex *ce;"):vk.index("static PlatRMutex tex_lock;")]
    preamble = r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include "nvn_api.h"
#include "platform/platform.h"
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"check line %d: %s\n",__LINE__,#x);exit(1); } } while (0)
typedef uint64_t VkSampler;
static unsigned calls, away, back, locked;
static dpool_ref received;
static int received_id;
static uint32_t reg_serial, slots_gen, samp_gen;
void rt_fatal(const char *fmt, ...) {
    CHECK(!calls && !locked);
    va_list ap;va_start(ap,fmt);vfprintf(stderr,fmt,ap);va_end(ap);exit(73);
}
void nvn_called(uint8_t *seen,const char *name) {(void)seen;CHECK(!strcmp(name,"nvnTexturePoolRegisterImage"));}
int kern_core_away(void) {away++;return 1;}
void kern_core_back(int *value) {CHECK(*value==1);back++;}
int plat_mem_read(uint64_t addr,void *out,size_t size) {
    if (addr<4096) return 0;
    memcpy(out,(const void *)(uintptr_t)addr,size);return 1;
}
static void lock(void) {CHECK(!locked);locked=1;}
static void unlock(void) {CHECK(locked);locked=0;}
static void log_limited(volatile int32_t *n,const char *fmt,...) {(void)n;(void)fmt;exit(2);}
'''
    slot = r'''
static DescSlot stored;
static DescSlot *descriptor_slot(const dpool_ref *pool,int id,uint32_t limit,int create) {
    CHECK(locked && create && limit==(1u<<20));received=*pool;received_id=id;return &stored;
}
'''
    bridge = r'''
static void register_image(const dpool_ref *pool,int id,const tex_t *texture,const view_t *view) {
    CHECK(away==back+1);calls++;vk_texture_register(pool,id,texture,view);
}
const GfxBackend *gfx_backend(void) {
    static const GfxBackend backend={.texture_register=register_image};return &backend;
}
'''
    main = r'''
int main(int argc,char **argv) {
    CHECK(argc==2);
    _Alignas(32) uint8_t bytes[256]={0};
    pool_t backing={.magic=NVN_POOL_MAGIC,.mem=bytes,.size=sizeof bytes};
    dpool_t pool={.pool=&backing,.off=32,.n=7};
    tex_t tex={.magic=NVN_TEX_MAGIC,.serial=19,.mem=bytes,
               .d={.format=NVN_FORMAT_RGBA8,.target=NVN_TEXTURE_TARGET_2D_ARRAY,.levels=5},.L={.layers=8}};
    view_t view={.set_levels=1,.base_level=2,.nlevels=1,.set_layers=1,.min_layer=3,.nlayers=2,
                 .set_format=1,.format=NVN_FORMAT_RGBA8,.set_target=1,.target=NVN_TEXTURE_TARGET_2D_ARRAY};
    Cpu c={0};c.x[0]=(uintptr_t)&pool;c.x[1]=6;c.x[2]=(uintptr_t)&tex;c.x[3]=(uintptr_t)&view;
    const char *mode=argv[1];
    if (!strcmp(mode,"null-pool")) c.x[0]=0;
    else if (!strcmp(mode,"bad-pool-pointer")) c.x[0]=1;
    else if (!strcmp(mode,"bad-backing-pointer")) pool.pool=(const pool_t *)1;
    else if (!strcmp(mode,"null-backing")) pool.pool=NULL;
    else if (!strcmp(mode,"pool-magic")) backing.magic=0;
    else if (!strcmp(mode,"pool-null-memory")) backing.mem=NULL;
    else if (!strcmp(mode,"pool-negative-offset")) pool.off=-32;
    else if (!strcmp(mode,"pool-alignment")) pool.off=1;
    else if (!strcmp(mode,"pool-size")) backing.size=255;
    else if (!strcmp(mode,"pool-address-overflow")) backing.mem=(uint8_t *)(uintptr_t)(UINT64_MAX-31);
    else if (!strcmp(mode,"pool-zero-count")) pool.n=0;
    else if (!strcmp(mode,"pool-over-count")) pool.n=(1<<20)+1;
    else if (!strcmp(mode,"negative-id")) c.x[1]=UINT32_MAX;
    else if (!strcmp(mode,"outside-id")) c.x[1]=7;
    else if (!strcmp(mode,"null-texture")) c.x[2]=0;
    else if (!strcmp(mode,"bad-texture-pointer")) c.x[2]=1;
    else if (!strcmp(mode,"texture-magic")) tex.magic=0;
    else if (!strcmp(mode,"texture-serial")) tex.serial=0;
    else if (!strcmp(mode,"texture-zero-levels")) tex.d.levels=0;
    else if (!strcmp(mode,"texture-zero-layers")) tex.L.layers=0;
    else if (!strcmp(mode,"bad-view-pointer")) c.x[3]=1;
    else if (!strcmp(mode,"mip-base")) view.base_level=5;
    else if (!strcmp(mode,"mip-count")) view.nlevels=4;
    else if (!strcmp(mode,"layer-base")) view.min_layer=8;
    else if (!strcmp(mode,"layer-count")) view.nlayers=6;
    else if (!strcmp(mode,"format")) view.format=NVN_FORMAT_R8;
    else if (!strcmp(mode,"target")) view.target=NVN_TEXTURE_TARGET_3D;
    else if (!strcmp(mode,"null-view")) c.x[3]=0;
    else if (!strcmp(mode,"zero-counts")) view.nlevels=view.nlayers=0;
    else if (!strcmp(mode,"first-id")) c.x[1]=0;
    else if (!strcmp(mode,"srgb-linear")) tex.d.format=NVN_FORMAT_RGBA8_SRGB;
    else if (!strcmp(mode,"srgb-multisample")) {tex.d.format=NVN_FORMAT_RGBA8_SRGB;tex.d.samples=4;}
    else CHECK(!strcmp(mode,"snapshot"));
    const tex_t expected_tex=tex;const view_t expected_view=view;const Cpu expected_cpu=c;
    nvnp_nvnTexturePoolRegisterImage(&c);
    CHECK(!memcmp(&c,&expected_cpu,sizeof c));
    CHECK(calls==1 && away==1 && back==1 && !locked);
    CHECK(received.addr==(uintptr_t)bytes+32 && received.count==7 && received_id==(int32_t)c.x[1]);
    CHECK(stored.kind==DESC_TEXTURE && stored.serial==1 && stored.u.tex.used && !stored.u.tex.ce);
    CHECK(!memcmp(&stored.u.tex.t,&expected_tex,sizeof tex));
    if(c.x[3])CHECK(stored.u.tex.has_view && !memcmp(&stored.u.tex.v,&expected_view,sizeof view));
    else {view_t empty={0};CHECK(!stored.u.tex.has_view && !memcmp(&stored.u.tex.v,&empty,sizeof empty));}
    // Both objects can be changed immediately after registration; the descriptor owns its values.
    memset(&tex,0,sizeof tex);memset(&view,0,sizeof view);
    CHECK(!memcmp(&stored.u.tex.t,&expected_tex,sizeof tex));
    if(c.x[3])CHECK(!memcmp(&stored.u.tex.v,&expected_view,sizeof view));
    // Re-registering through another wrapper of the same backing replaces the old view and serial.
    dpool_t alias=pool;tex=expected_tex;tex.serial=20;c.x[0]=(uintptr_t)&alias;c.x[3]=0;
    nvnp_nvnTexturePoolRegisterImage(&c);
    CHECK(calls==2 && away==2 && back==2 && stored.serial==2 && slots_gen==2 && samp_gen==2);
    CHECK(stored.u.tex.t.serial==20 && !stored.u.tex.has_view);
    view_t empty={0};CHECK(!memcmp(&stored.u.tex.v,&empty,sizeof empty));
    puts("image registration check passed");return 0;
}
'''
    return (preamble + types + slot + function(vk, "static void descriptor_registered(") +
            function(vk, "void vk_texture_register(") + bridge +
            function(obj, "NVN_API(nvnTexturePoolRegisterImage)") + main)


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Windows Clang required")
class ImageRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hwaoi-image-register-")
        path = Path(cls.tmp.name) / "fixture.c"
        path.write_text(source(), encoding="utf-8")
        cls.exes = []
        for opt in ("-O0", "-O2"):
            exe = path.with_name(f"fixture{opt}.exe")
            result = subprocess.run([str(CC), opt, "-Wall", "-Wextra", "-Werror", "-march=x86-64-v3",
                                     "-I", str(ROOT / "runtime"), str(path), "-o", str(exe)],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                cls.tmp.cleanup()
                raise AssertionError(result.stdout + result.stderr)
            cls.exes.append(exe)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_snapshots_views_replacement_and_boundary_ids(self):
        for exe in self.exes:
            for case in ("snapshot", "null-view", "zero-counts", "first-id", "srgb-linear"):
                with self.subTest(opt=exe.stem, case=case):
                    result = subprocess.run([str(exe), case], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("image registration check passed", result.stdout)

    def test_invalid_objects_ranges_and_unsupported_views_fail_before_registration(self):
        cases = ("null-pool", "bad-pool-pointer", "bad-backing-pointer", "null-backing", "pool-magic",
                 "pool-null-memory", "pool-negative-offset", "pool-alignment", "pool-size",
                 "pool-address-overflow", "pool-zero-count", "pool-over-count", "negative-id", "outside-id",
                 "null-texture", "bad-texture-pointer", "texture-magic", "texture-serial", "texture-zero-levels",
                 "texture-zero-layers", "bad-view-pointer", "mip-base", "mip-count", "layer-base", "layer-count",
                 "format", "target", "srgb-multisample")
        for exe in self.exes:
            for case in cases:
                with self.subTest(opt=exe.stem, case=case):
                    result = subprocess.run([str(exe), case], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
                    self.assertIn("nvn: RegisterImage", result.stderr)


if __name__ == "__main__":
    unittest.main()
