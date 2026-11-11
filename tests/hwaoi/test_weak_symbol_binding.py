"""Exercise the real loader's default-visible weak binding with synthetic modules."""
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


def fixture(mutation=False):
    text = (ROOT / "runtime/loader.c").read_text()
    types = text[text.index("enum { STB_GLOBAL"):text.index("static Mod mods[")]
    code = "\n".join(function(text, s) for s in (
        "static const char *sym_name(", "static int sym_defined(", "static uint32_t fnv(",
        "static void make_hash(", "static uint64_t module_symbol(", "static uint64_t find_symbol(",
        "static uint64_t defined_guest_symbol(", "static Res resolve("))
    if mutation:
        code = code.replace("defined_guest_symbol(m, si)", "m->pub.base + m->syms[si].value")
    return r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "host.h"
#define MOD_COUNT 3
#define K_COUNT 4
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"line %d: %s\n",__LINE__,#x);exit(1); } } while(0)
''' + types + r'''
static Mod mods[MOD_COUNT]; static int nmods=3; static uint64_t stubs;
static int override_on, observer_on; static uint64_t observed_original;
static void fake(Cpu *c) { (void)c; }
static void unresolved_stub(Cpu *c) { (void)c; }
GuestFn hostfn_observer(const char *n) { (void)n; return observer_on ? fake : NULL; }
GuestFn hostfn_override(const char *n) { (void)n; return override_on ? fake : NULL; }
uint64_t hostfn_register(const char *n, GuestFn f) { (void)n; return f==fake ? 0xf000 : 0xf100; }
uint64_t hostfn_register_observer(const char *n,GuestFn f,uint64_t original) {
    (void)n;(void)f;observed_original=original;return 0xf200;
}
static void mark_used(int observer,const char *name) {(void)observer;(void)name;}
void rt_log(const char *fmt,...) {(void)fmt;}
void rt_fatal(const char *fmt,...) {(void)fmt;abort();}
''' + code + r'''
static Elf64Sym symbols[3][4]; static Res plain[3][4],jump[3][4];
static const char names[]="\0singleton\0guard\0";
static void reset(void) {
    for(int i=0;i<3;i++) free(mods[i].hash);
    memset(mods,0,sizeof mods); memset(symbols,0,sizeof symbols);
    memset(plain,0,sizeof plain);memset(jump,0,sizeof jump);
    override_on=observer_on=0;stubs=0;observed_original=0;
    for(int i=0;i<3;i++) {
        mods[i].pub.base=0x100000ull*(i+1); mods[i].pub.name="fixture";
        mods[i].strtab=names;mods[i].syms=symbols[i];mods[i].nsyms=4;
        mods[i].res_plain=plain[i];mods[i].res_jump=jump[i];
        symbols[i][1]=(Elf64Sym){.name=1,.info=0x21,.shndx=1,.value=0x80,.size=8};
        symbols[i][2]=(Elf64Sym){.name=11,.info=0x21,.shndx=1,.value=0x88,.size=8};
    }
}
static void hash_all(void) {for(int i=0;i<3;i++){free(mods[i].hash);mods[i].hash=NULL;make_hash(&mods[i]);}}
static uint64_t own(int m,int s) {return mods[m].pub.base+symbols[m][s].value;}
static void cases(void) {
    reset();hash_all();
    for(int m=0;m<3;m++) for(int s=1;s<=2;s++) {
        CHECK(resolve(&mods[m],s,0).value==own(0,s));
        CHECK(resolve(&mods[m],s,1).value==own(0,s));
    }
    // A canonical guard and data object use the same module, even with value 0.
    reset();symbols[0][1].value=0;hash_all();CHECK(resolve(&mods[2],1,0).value==mods[0].pub.base);
    // Runtime scope is first eligible definition, not static link strength sorting.
    reset();symbols[1][1].info=0x11;hash_all();CHECK(resolve(&mods[2],1,0).value==own(0,1));
    reset();symbols[0][1].info=0x11;hash_all();CHECK(resolve(&mods[2],1,0).value==own(0,1));
    for(unsigned vis=1;vis<=3;vis++) {
        reset();symbols[2][1].other=(uint8_t)vis;hash_all();
        CHECK(resolve(&mods[2],1,0).value==own(2,1));
    }
    for(unsigned vis=1;vis<=2;vis++) {
        reset();symbols[0][1].other=(uint8_t)vis;hash_all();
        CHECK(resolve(&mods[2],1,0).value==own(1,1));
    }
    reset();symbols[0][1].info=0x01;hash_all();CHECK(resolve(&mods[2],1,0).value==own(1,1));
    reset();symbols[2][1].info=0x01;hash_all();CHECK(resolve(&mods[2],1,0).value==own(2,1));
    reset();symbols[0][1].other=3;hash_all();CHECK(resolve(&mods[2],1,0).value==own(0,1));
    reset();symbols[0][1].shndx=symbols[1][1].shndx=0;hash_all();
    CHECK(resolve(&mods[2],1,0).value==own(2,1));
    reset();for(int i=0;i<3;i++)symbols[i][1].shndx=0;hash_all();
    CHECK(resolve(&mods[2],1,0).kind==RES_WEAK && resolve(&mods[2],1,0).value==0);
    reset();for(int i=0;i<3;i++)symbols[i][1].shndx=0;symbols[2][1].info=0x11;hash_all();
    CHECK(resolve(&mods[2],1,0).kind==RES_STUB && stubs==1);
    reset();hash_all();override_on=1;CHECK(resolve(&mods[2],1,0).kind==RES_OVERRIDE);
    observer_on=1;CHECK(resolve(&mods[2],1,1).kind==RES_OBSERVER && observed_original==own(0,1));
    CHECK(resolve(&mods[2],1,0).kind==RES_OVERRIDE); // separate JUMP_SLOT cache
}
static uint32_t rng=0x582fa043;
static uint32_t rnd(void){rng^=rng<<13;rng^=rng>>17;rng^=rng<<5;return rng;}
int main(void) {
    (void)defined_guest_symbol;
    cases();
    for(unsigned k=0;k<4096;k++) {
        reset();
        for(int i=0;i<3;i++)for(int s=1;s<=2;s++) {
            symbols[i][s].info=(uint8_t)((rnd()%3)<<4)|1;
            symbols[i][s].other=(uint8_t)(rnd()%4);
            symbols[i][s].shndx=(uint16_t)(rnd()%2);
        }
        symbols[2][1].info=0x21;symbols[2][1].other=0;symbols[2][1].shndx=1;
        hash_all();uint64_t expected=own(2,1);
        for(int i=0;i<3;i++) {
            Elf64Sym *s=&symbols[i][1];unsigned b=s->info>>4,v=s->other&3;
            if(s->shndx && (b==1 || b==2) && (v==0 || v==3)){expected=own(i,1);break;}
        }
        CHECK(resolve(&mods[2],1,0).value==expected);
        CHECK(resolve(&mods[2],1,1).value==expected);
    }
    reset();puts("weak binding checks passed");return 0;
}
'''


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Windows Clang required")
class WeakSymbolTests(unittest.TestCase):
    def test_real_resolution_and_negative_control(self):
        with tempfile.TemporaryDirectory(prefix="hwaoi-weak-") as folder:
            for opt in ("-O0", "-O2"):
                for mutation in (False, True):
                    with self.subTest(opt=opt, mutation=mutation):
                        path=Path(folder)/"fixture.c";exe=Path(folder)/"fixture.exe"
                        path.write_text(fixture(mutation),encoding="utf-8")
                        build=subprocess.run([str(CC),opt,"-std=c11","-Wall","-Wextra","-Werror",
                            "-I",str(ROOT/"runtime"),str(path),"-o",str(exe)],capture_output=True,text=True)
                        self.assertEqual(build.returncode,0,build.stderr)
                        run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=10)
                        self.assertEqual(run.returncode==0,not mutation,run.stdout+run.stderr)


if __name__ == "__main__":
    unittest.main()
