// Native fixture for runtime/aoc.c (built with -DAOC_NO_OVERRIDES): host stubs for the runtime and platform layer,
// then Count/List, mounts, reads, entry types, directory listings and the first-open integrity check.
// tests/hwaoi/test_aoc.py installs add-ons 2 and 5 holding top.bin, data/one.bin and data/deep/two.bin.
#include "fs.h"
#include "host.h"
#include "platform/platform.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

void rt_fatal(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fputs("FATAL: ", stdout);
    vprintf(fmt, ap);
    va_end(ap);
    fputc('\n', stdout);
    fflush(stdout);
    exit(70);
}
void rt_log(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fputc('\n', stdout);
}
void mem_thaw(uint64_t a, uint64_t n) { (void)a, (void)n; }
const RtConfig *rt_config(void) { return NULL; }
void fs_trace(const char *what, const char *path) { (void)what, (void)path; }
int romfs_owns(const char *path) { return path && !strncmp(path, "rom:", 4); }
int fs_host_mounted(const char *name) { return name && !strcmp(name, "save"); }

// ---- platform stubs (single-threaded fixture)
void plat_mutex_lock(PlatMutex *m) { (void)m; }
void plat_mutex_unlock(PlatMutex *m) { (void)m; }
void plat_mutex_lock_shared(PlatMutex *m) { (void)m; }
void plat_mutex_unlock_shared(PlatMutex *m) { (void)m; }
void plat_once(PlatOnce *o, void (*fn)(void *), void *arg) {
    if (!o->state) {
        o->state = 1;
        fn(arg);
    }
}
struct PlatFile { FILE *f; };
PlatFile *plat_file_open(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    PlatFile *p = malloc(sizeof *p);
    p->f = f;
    return p;
}
void plat_file_close(PlatFile *f) {
    fclose(f->f);
    free(f);
}
int64_t plat_file_pread(PlatFile *f, void *buf, uint32_t n, uint64_t off) {
    if (_fseeki64(f->f, (long long)off, SEEK_SET)) return -1;
    return (int64_t)fread(buf, 1, n, f->f);
}
int plat_file_id(PlatFile *f, PlatFileId *out) {
    memset(out, 0, sizeof *out);
    long long here = _ftelli64(f->f);
    _fseeki64(f->f, 0, SEEK_END);
    out->size = (uint64_t)_ftelli64(f->f);
    _fseeki64(f->f, here, SEEK_SET);
    return 1;
}
uint8_t *plat_file_read(const char *path, uint64_t *size) {
    PlatFile *f = plat_file_open(path);
    if (!f) return NULL;
    PlatFileId id;
    plat_file_id(f, &id);
    uint8_t *b = malloc(id.size + 1);
    *size = (uint64_t)plat_file_pread(f, b, (uint32_t)id.size, 0);
    plat_file_close(f);
    return b;
}
int plat_fs_kind(const char *path) {
    DWORD a = GetFileAttributesA(path);
    return a == INVALID_FILE_ATTRIBUTES ? PLAT_FS_MISSING : (a & FILE_ATTRIBUTE_DIRECTORY) ? PLAT_FS_DIR : PLAT_FS_FILE;
}

typedef struct { char name[769]; uint8_t pad0[3]; int8_t type; uint8_t pad1[3]; int64_t size; } Entry;

static int failures;
#define CHECK(c, ...) do { if (!(c)) { printf("FAIL %d: ", __LINE__); printf(__VA_ARGS__); printf("\n"); failures++; } } while (0)

static void read_all(const char *path, const char *want) {
    uint64_t h = 0, got = 0;
    int64_t size = -1;
    char buf[64] = {0};
    CHECK(aoc_owns(path), "owns %s", path);
    CHECK(aoc_open(&h, path, FS_OPEN_READ) == 0 && aoc_handle(h), "open %s", path);
    CHECK(aoc_size(&size, h) == 0 && size == (int64_t)strlen(want), "size %s", path);
    CHECK(aoc_read(&got, h, 0, buf, sizeof buf) == 0 && got == strlen(want) && !memcmp(buf, want, got), "read %s", path);
    CHECK(aoc_read(&got, h, 2, buf, 1) == 0 && got == 1 && buf[0] == want[2], "offset read %s", path);
    CHECK(aoc_read(&got, h, size, buf, 4) == 0 && got == 0, "read at end %s", path);
    CHECK(aoc_read(&got, h, size + 1, buf, 4) == FS_R_INVALID, "read past end %s", path);
    aoc_close(h);
}

int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "tampered")) {   // top.bin was changed after install: first open is fatal
        aoc_mount("dlc", 2);
        uint64_t h;
        aoc_open(&h, "dlc:/top.bin", FS_OPEN_READ);
        printf("tampered file opened\n");
        return 0;
    }
    int32_t list[8] = {0};
    uint64_t h;
    CHECK(aoc_count() == 2, "count %d", aoc_count());
    CHECK(aoc_list(list, 0, 8) == 2 && list[0] == 2 && list[1] == 5, "list %d %d", list[0], list[1]);
    CHECK(aoc_list(list, 1, 8) == 1 && list[0] == 5, "list offset");
    CHECK(aoc_list(list, 0, 1) == 1 && list[0] == 2, "list count");
    CHECK(aoc_list(list, 2, 8) == 0, "list past end");

    CHECK(aoc_mount("dlc", 7) == FS_R_TARGET_NOT_FOUND, "uninstalled index");
    CHECK(aoc_mount("bad:name", 2) == FS_R_INVALID_ARGUMENT && aoc_mount("", 2) == FS_R_INVALID_ARGUMENT, "bad names");
    CHECK(aoc_mount("rom", 2) == FS_R_ALREADY_EXISTS && aoc_mount("save", 2) == FS_R_ALREADY_EXISTS, "taken names");
    CHECK(aoc_mount("dlc", 2) == 0 && aoc_mount("dlc", 5) == FS_R_ALREADY_EXISTS, "mount");
    CHECK(aoc_mount("dlc5", 5) == 0, "second add-on mount");
    CHECK(!aoc_owns("rom:/top.bin") && !aoc_owns("nope:/x") && aoc_owns("dlc:/x"), "owns");

    read_all("dlc:/top.bin", "top");
    read_all("dlc:/data/one.bin", "0123456789");
    read_all("dlc5://data/deep/two.bin", "two");   // a doubled root separator is accepted like rom:
    CHECK(aoc_open(&h, "dlc5:/data//deep/two.bin", FS_OPEN_READ) == FS_R_PATH_NOT_FOUND, "empty interior component");
    CHECK(aoc_open(&h, "dlc:/missing.bin", FS_OPEN_READ) == FS_R_PATH_NOT_FOUND, "missing");
    CHECK(aoc_open(&h, "dlc:/data", FS_OPEN_READ) == FS_R_PATH_NOT_FOUND, "open a folder");
    CHECK(aoc_open(&h, "dlc:/../top.bin", FS_OPEN_READ) == FS_R_PATH_NOT_FOUND, "traversal");
    CHECK(aoc_open(&h, "dlc:/top.bin", FS_OPEN_READ | FS_OPEN_WRITE) == FS_R_UNSUPPORTED_OPEN, "write open");

    int type = -1;
    CHECK(aoc_entry_type(&type, "dlc:/top.bin") == 0 && type == FS_ENTRY_FILE, "file type");
    CHECK(aoc_entry_type(&type, "dlc:/data") == 0 && type == FS_ENTRY_DIRECTORY, "dir type");
    CHECK(aoc_entry_type(&type, "dlc:/") == 0 && type == FS_ENTRY_DIRECTORY, "root type");
    CHECK(aoc_entry_type(&type, "dlc:/dat") == FS_R_PATH_NOT_FOUND, "prefix is not a folder");

    Entry e[8];
    int64_t n = -1;
    uint64_t d;
    CHECK(aoc_open_directory(&d, "dlc:/data", 3) == 0 && aoc_dir_handle(d), "open dir");
    CHECK(aoc_read_directory(&n, e, d, 8) == 0 && n == 2, "list data: %lld", (long long)n);
    CHECK(n == 2 && !strcmp(e[0].name, "deep") && e[0].type == FS_ENTRY_DIRECTORY && !strcmp(e[1].name, "one.bin") &&
          e[1].type == FS_ENTRY_FILE && e[1].size == 10, "data entries");
    CHECK(aoc_read_directory(&n, e, d, 8) == 0 && n == 0, "listing is consumed");
    aoc_close_directory(d);
    CHECK(aoc_open_directory(&d, "dlc:/", 2) == 0 && aoc_read_directory(&n, e, d, 8) == 0 && n == 1 &&
          !strcmp(e[0].name, "top.bin"), "root files only");
    aoc_close_directory(d);
    CHECK(aoc_open_directory(&d, "dlc:/", 1) == 0 && aoc_read_directory(&n, e, d, 8) == 0 && n == 1 &&
          !strcmp(e[0].name, "data"), "root folders only");
    aoc_close_directory(d);
    CHECK(aoc_open_directory(&d, "dlc:/top.bin", 3) == FS_R_PATH_NOT_FOUND, "open a file as a folder");

    CHECK(aoc_unmount("dlc") && !aoc_owns("dlc:/top.bin") && !aoc_unmount("dlc"), "unmount");
    CHECK(aoc_mount("dlc", 5) == 0, "remount another add-on under a freed name");
    printf("%s (%d failures)\n", failures ? "FAILED" : "ok", failures);
    return failures != 0;
}
