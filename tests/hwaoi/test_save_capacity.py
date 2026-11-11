"""Exercise the actual host save query and Windows space reader, without a ROM."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")


def function(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


@unittest.skipUnless(os.name == "nt" and CC.is_file(), "Existing Windows Clang required")
class SaveCapacityTests(unittest.TestCase):
    def test_capacity_identity_outputs_errors_and_native_volume(self):
        fs = (ROOT / "runtime/fs_host.c").read_text(encoding="utf-8")
        win = (ROOT / "runtime/platform/win32.c").read_text(encoding="utf-8")
        source = r'''
#include "host.h"
#include "fs.h"
#include "platform/platform.h"
#include <windows.h>
#include <stdlib.h>
#include <stdio.h>
#define WPATH 8192
static void check(int value) { if (!value) abort(); }
'''
        source += function(win, "static int widen(") + "\n"
        source += function(win, "static int fs_err(") + "\n"
        source += function(win, "int plat_fs_space(") + "\n"
        source += r'''
static uint64_t space_total = 0x100000001ull, space_available = 0x10000;
static int space_error, reads, initializations;
static const char *save_root = "selected-save-root";
static int fake_space(const char *path, uint64_t *total, uint64_t *available) {
    check(!strcmp(path, save_root)); reads++;
    if (space_error) return space_error;
    *total = space_total; *available = space_available; return 0;
}
static int my_uid(const uint64_t *uid) { return uid && uid[0] == 123 && uid[1] == 456; }
static void ready(void) { initializations++; }
static uint32_t fs_result(int e) { return e ? FS_R_INVALID_ARGUMENT : 0; }
void rt_log(const char *fmt, ...) { (void)fmt; }
#define plat_fs_space fake_space
'''
        source += function(fs, "static uint32_t get_save_size(") + "\n"
        source += function(fs, "static void ov_get_save_size(") + "\n"
        source += r'''
#undef plat_fs_space
int main(void) {
    uint64_t uid[2] = {123,456}, other[2] = {123,457};
    int64_t data = -11, journal = -22;
    check(get_save_size(NULL, &journal, uid) == FS_R_INVALID_ARGUMENT && journal == -22);
    check(get_save_size(&data, NULL, uid) == FS_R_INVALID_ARGUMENT && data == -11);
    check(get_save_size(&data, &journal, NULL) == FS_R_INVALID_ARGUMENT);
    check(get_save_size(&data, &data, uid) == FS_R_INVALID_ARGUMENT && data == -11);
    check(get_save_size(&data, &journal, other) == FS_R_TARGET_NOT_FOUND && data == -11 && journal == -22);
    check(!reads && !initializations);
    space_error = PLAT_FS_ERROR;
    check(get_save_size(&data, &journal, uid) == FS_R_INVALID_ARGUMENT && data == -11 && journal == -22);
    space_error = 0; space_total = UINT64_MAX;
    check(get_save_size(&data, &journal, uid) == FS_R_INVALID_ARGUMENT && data == -11 && journal == -22);
    space_total = 10; space_available = 11;
    check(get_save_size(&data, &journal, uid) == FS_R_INVALID_ARGUMENT && data == -11 && journal == -22);
    space_total = 0x100000001ull; space_available = 0;
    Cpu c = {0}; c.x[0] = (uintptr_t)&data; c.x[1] = (uintptr_t)&journal; c.x[2] = (uintptr_t)uid;
    c.x[19] = 0x1234; c.sp = 0x5432;
    ov_get_save_size(&c);
    check(c.x[0] == 0 && data == 0x100000001ll && journal == 0 && c.x[19] == 0x1234 && c.sp == 0x5432);
    uint64_t total = 0, available = 0;
    check(plat_fs_space(".", &total, &available) == 0 && total > 0 && available <= total);
    uint64_t saved_total = total, saved_available = available;
    check(plat_fs_space("./no-such-save-capacity-fixture-directory", &total, &available) != 0);
    check(total == saved_total && available == saved_available);
    check(plat_fs_space(NULL, &total, &available) != 0);
    check(plat_fs_space(".", NULL, &available) != 0);
    puts("save capacity and native volume checks passed");
}
'''
        with tempfile.TemporaryDirectory(prefix="save-capacity-") as temporary:
            file = Path(temporary) / "fixture.c"
            file.write_text(source, encoding="utf-8")
            for optimization in ("0", "2"):
                exe = Path(temporary) / f"fixture-{optimization}.exe"
                build = subprocess.run([
                    str(CC), "-std=gnu11", f"-O{optimization}", "-Wall", "-Wextra", "-Werror",
                    "-march=x86-64-v3", "-I", str(ROOT / "runtime"), str(file), "-o", str(exe),
                ], capture_output=True, text=True, timeout=60)
                self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
                run = subprocess.run([str(exe)], cwd=temporary, capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
