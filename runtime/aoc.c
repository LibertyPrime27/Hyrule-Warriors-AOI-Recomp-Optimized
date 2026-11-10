// HWAoI add-on content (DLC): nn::aoc Count/List and read-only nn::fs::MountAddOnContent mounts.
//
// Each installed add-on is a folder <aoc root>/<index>/ holding manifest.tsv and romfs/<path> files:
//   line 1:   HWAOI-AOC<TAB>1<TAB><add-on title id, 16 hex><TAB><NCM version>
//   others:   <sha256, 64 hex><TAB><size><TAB><RomFS path>, sorted by path bytes, each path once.
// tools/hwaoi_aoc.py writes these folders from a NUP's addon/addon_update packages or from an extracted
// add-on RomFS. Only manifest paths are visible; a file whose size differs from its row fails at mount time, and its
// SHA-256 is checked on first open, like the game RomFS. A mismatch is fatal: altered DLC data never reaches the game.
//
// The aoc root is HWAOI_AOC_DIR, else "aoc" beside the game RomFS folder (rt_config()->romfs/../aoc). With no root,
// or HWAOI_AOC=0, no add-on is installed and Count/List report zero, as before.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fs.h"
#include "host.h"
#include "platform/platform.h"
#include "romfs_path.h"
#include "sha256.h"

#define AOC_MAX 64              // installed add-ons
#define AOC_INDEX_MAX 2047      // AddOnContentIndex: id = application id + 0x1000 + index
#define AOC_MOUNTS 16
#define AOC_FILES 1024          // open add-on file handles
#define AOC_DIRS 64             // open add-on directory handles
#define AOC_READ_CHUNK (64u << 20)

typedef struct { char *path; uint64_t size; uint8_t sha[32]; int verified; } AocFile;
typedef struct { int index; uint64_t title; uint32_t version; char root[1100]; AocFile *files; size_t n; } Aoc;
typedef struct { char name[32]; int aoc; } AocMount;    // aoc: slot + 1, 0 = free
typedef struct { PlatFile *f; uint32_t aoc, file; } AocHandle;   // f NULL = free
typedef struct { char *name; int is_dir; uint64_t size; } AocListed;
typedef struct { AocListed *e; int n, next, mode, used; } AocDir;

static Aoc aocs[AOC_MAX];
static size_t naoc;
static AocMount mounts[AOC_MOUNTS];
static AocHandle handles[AOC_FILES];
static AocDir dirs[AOC_DIRS];
static PlatMutex lock = PLAT_MUTEX_INIT;
static PlatMutex verify_lock = PLAT_MUTEX_INIT;
static PlatOnce once;
static char root_dir[1024];

static void need(int c, const char *m) {
    if (!c) rt_fatal("aoc: %s", m);
}

static int hexval(char c) {
    return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
}

static int parse_sha(const char *s, uint8_t out[32]) {
    for (int i = 0; i < 32; i++) {
        int hi = hexval(s[2 * i]), lo = hi < 0 ? -1 : hexval(s[2 * i + 1]);
        if (lo < 0) return 0;
        out[i] = (uint8_t)(hi << 4 | lo);
    }
    return s[64] == '\t';
}

// The aoc root: HWAOI_AOC_DIR, else <romfs>/../aoc. Empty when DLC is disabled.
static void root_path(void) {
    const char *off = getenv("HWAOI_AOC");
    if (off && !strcmp(off, "0")) return;
    const char *e = getenv("HWAOI_AOC_DIR");
    if (e && *e) {
        need(snprintf(root_dir, sizeof root_dir, "%s", e) < (int)sizeof root_dir, "HWAOI_AOC_DIR is too long");
        return;
    }
    const RtConfig *cfg = rt_config();
    if (!cfg || !cfg->romfs || !*cfg->romfs) return;
    char romfs[1024];
    need(snprintf(romfs, sizeof romfs, "%s", cfg->romfs) < (int)sizeof romfs, "RomFS path is too long");
    size_t n = strlen(romfs);
    while (n > 1 && (romfs[n - 1] == '/' || romfs[n - 1] == '\\')) romfs[--n] = 0;
    char *sep = plat_last_sep(romfs);
    if (sep) *sep = 0;
    else strcpy(romfs, ".");
    need(snprintf(root_dir, sizeof root_dir, "%s/aoc", romfs) < (int)sizeof root_dir, "aoc path is too long");
}

// Parses <dir>/manifest.tsv into a. 0 (logged) when the folder is not an add-on; malformed manifests are fatal.
static int load_manifest(Aoc *a, const char *dir, int index) {
    char path[1200];
    need(snprintf(path, sizeof path, "%s/manifest.tsv", dir) < (int)sizeof path, "manifest path is too long");
    uint64_t size = 0;
    char *text = (char *)plat_file_read(path, &size);
    if (!text) return 0;
    need(size < (256u << 20), "manifest is too large");
    char *buf = realloc(text, (size_t)size + 1);
    need(buf != NULL, "manifest allocation");
    buf[size] = 0;
    char *line = buf, *next;
    *a = (Aoc){.index = index};
    need(snprintf(a->root, sizeof a->root, "%s/romfs", dir) < (int)sizeof a->root, "add-on path is too long");
    size_t cap = 0;
    int first = 1;
    for (; line && *line; line = next) {
        next = strchr(line, '\n');
        if (next) *next++ = 0;
        size_t k = strlen(line);
        if (k && line[k - 1] == '\r') line[--k] = 0;
        if (!k) continue;
        if (first) {
            char title[17] = {0};
            unsigned long long version = 0;
            if (sscanf(line, "HWAOI-AOC\t1\t%16[0-9A-Fa-f]\t%llu", title, &version) != 2 || strlen(title) != 16 ||
                version > UINT32_MAX)
                rt_fatal("aoc: %s: first line must be HWAOI-AOC<TAB>1<TAB><title id><TAB><version>", path);
            a->title = strtoull(title, NULL, 16);
            a->version = (uint32_t)version;
            first = 0;
            continue;
        }
        AocFile f = {0};
        char *size_s = line + 65, *end = NULL;
        if (k < 68 || !parse_sha(line, f.sha)) rt_fatal("aoc: %s: bad row %s", path, line);
        f.size = strtoull(size_s, &end, 10);
        if (end == size_s || *end != '\t' || !romfs_path_valid_relative(end + 1))
            rt_fatal("aoc: %s: bad row %s", path, line);
        f.path = strdup(end + 1);
        need(f.path != NULL, "manifest allocation");
        if (a->n && strcmp(a->files[a->n - 1].path, f.path) >= 0)
            rt_fatal("aoc: %s: rows are not sorted by path or repeat %s", path, f.path);
        if (a->n == cap) {
            cap = cap ? cap * 2 : 256;
            a->files = realloc(a->files, cap * sizeof *a->files);
            need(a->files != NULL, "manifest allocation");
        }
        a->files[a->n++] = f;
    }
    free(buf);
    if (first) rt_fatal("aoc: %s is empty", path);
    if (!a->n) rt_fatal("aoc: %s lists no files", path);
    // Cheap start check: every listed file is present with its size. Content is hashed on first open.
    for (size_t i = 0; i < a->n; i++) {
        char p[2400];
        need(snprintf(p, sizeof p, "%s/%s", a->root, a->files[i].path) < (int)sizeof p, "add-on file path is too long");
        PlatFile *h = plat_file_open(p);
        PlatFileId id;
        int ok = h && plat_file_id(h, &id) && id.size == a->files[i].size;
        if (h) plat_file_close(h);
        if (!ok) rt_fatal("aoc: add-on %d file %s is missing or has the wrong size (reinstall the DLC)", index, p);
    }
    return 1;
}

static int aoc_order(const void *x, const void *y) {
    return ((const Aoc *)x)->index - ((const Aoc *)y)->index;
}

static void init_once(void *param) {
    (void)param;
    root_path();
    if (!*root_dir) {
        rt_log("aoc: add-on content disabled");
        return;
    }
    if (plat_fs_kind(root_dir) != PLAT_FS_DIR) {
        rt_log("aoc: no add-on content (%s does not exist)", root_dir);
        return;
    }
    for (int index = 1; index <= AOC_INDEX_MAX; index++) {
        char dir[1100];
        snprintf(dir, sizeof dir, "%s/%d", root_dir, index);
        if (plat_fs_kind(dir) != PLAT_FS_DIR) continue;
        if (naoc == AOC_MAX) rt_fatal("aoc: more than %d add-ons under %s", AOC_MAX, root_dir);
        if (load_manifest(&aocs[naoc], dir, index)) {
            rt_log("aoc: add-on %d (title %016llX, version %u): %zu files", index,
                   (unsigned long long)aocs[naoc].title, aocs[naoc].version, aocs[naoc].n);
            naoc++;
        } else {
            rt_log("aoc: %s has no manifest.tsv; ignored", dir);
        }
    }
    qsort(aocs, naoc, sizeof *aocs, aoc_order);
    rt_log("aoc: %zu add-on(s) installed from %s", naoc, root_dir);
}

static void ready(void) { plat_once(&once, init_once, NULL); }

static Aoc *aoc_by_index(int index) {
    for (size_t i = 0; i < naoc; i++)
        if (aocs[i].index == index) return &aocs[i];
    return NULL;
}

// ---- nn::aoc
int aoc_count(void) {
    ready();
    return (int)naoc;
}

int aoc_list(int32_t *out, int offset, int count) {
    ready();
    if (offset < 0 || count < 0) rt_fatal("aoc: ListAddOnContent range %d+%d", offset, count);
    if (count && !out) rt_fatal("aoc: ListAddOnContent with a null buffer");
    int n = 0;
    for (size_t i = (size_t)offset; i < naoc && n < count; i++) out[n++] = aocs[i].index;
    return n;
}

// ---- mounts
static int mount_slot(const char *name) {   // under lock; slot or -1
    for (int i = 0; i < AOC_MOUNTS; i++)
        if (mounts[i].aoc && !strcmp(mounts[i].name, name)) return i;
    return -1;
}

int aoc_owns(const char *path) {
    if (!path) return 0;
    size_t n = strcspn(path, ":");
    if (!path[n] || !n || n >= 32) return 0;
    char name[32];
    memcpy(name, path, n);
    name[n] = 0;
    plat_mutex_lock_shared(&lock);
    int found = mount_slot(name) >= 0;
    plat_mutex_unlock_shared(&lock);
    return found;
}

uint32_t aoc_mount(const char *name, int index) {
    ready();
    if (!name || !*name || strlen(name) >= 32 || strchr(name, ':') || strchr(name, '/') || strchr(name, '\\'))
        return FS_R_INVALID_ARGUMENT;
    Aoc *a = aoc_by_index(index);
    if (!a) {
        rt_log("aoc: MountAddOnContent %s: add-on %d is not installed", name, index);
        return FS_R_TARGET_NOT_FOUND;
    }
    char probe[40];
    snprintf(probe, sizeof probe, "%s:", name);
    if (romfs_owns(probe) || fs_host_mounted(name)) return FS_R_ALREADY_EXISTS;
    plat_mutex_lock(&lock);
    if (mount_slot(name) >= 0) {
        plat_mutex_unlock(&lock);
        return FS_R_ALREADY_EXISTS;
    }
    for (int i = 0; i < AOC_MOUNTS; i++)
        if (!mounts[i].aoc) {
            snprintf(mounts[i].name, sizeof mounts[i].name, "%s", name);
            mounts[i].aoc = (int)(a - aocs) + 1;
            plat_mutex_unlock(&lock);
            rt_log("fs mount %s: -> add-on %d", name, index);
            return 0;
        }
    plat_mutex_unlock(&lock);
    rt_fatal("aoc: more than %d add-on mounts", AOC_MOUNTS);
}

int aoc_unmount(const char *name) {
    if (!name) return 0;
    plat_mutex_lock(&lock);
    int i = mount_slot(name);
    if (i < 0) {
        plat_mutex_unlock(&lock);
        return 0;
    }
    uint32_t slot = (uint32_t)mounts[i].aoc - 1;
    int open = 0;
    for (int j = 0; j < AOC_FILES; j++) open += handles[j].f && handles[j].aoc == slot;
    mounts[i] = (AocMount){0};
    plat_mutex_unlock(&lock);
    if (open) rt_log("aoc: unmount %s with %d file(s) still open", name, open);
    rt_log("fs unmount %s", name);
    return 1;
}

// "<mount>:/a/b" -> the add-on slot and "a/b" ("" for the root), or -1.
static int resolve(const char *path, const char **rel) {
    size_t n = strcspn(path, ":");
    if (!path[n] || path[n + 1] != '/' || !n || n >= 32) return -1;
    char name[32];
    memcpy(name, path, n);
    name[n] = 0;
    plat_mutex_lock_shared(&lock);
    int i = mount_slot(name), slot = i < 0 ? -1 : mounts[i].aoc - 1;
    plat_mutex_unlock_shared(&lock);
    if (slot < 0) return -1;
    const char *p = path + n + 1;
    while (*p == '/') p++;
    if (*p && !romfs_path_valid_relative(p)) return -1;
    *rel = p;
    return slot;
}

static long file_of(const Aoc *a, const char *p) {   // index of path p, or -1
    size_t lo = 0, hi = a->n;
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        int c = strcmp(a->files[mid].path, p);
        if (!c) return (long)mid;
        if (c < 0) lo = mid + 1;
        else hi = mid;
    }
    return -1;
}

// First row at or after "p/" (all rows for the root), so directory p's descendants are rows [first, ...) with
// that prefix.
static size_t first_under(const Aoc *a, const char *prefix) {
    size_t lo = 0, hi = a->n;
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        if (strcmp(a->files[mid].path, prefix) < 0) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

static int is_dir(const Aoc *a, const char *p, char *prefix, size_t cap) {
    if (!*p) {
        *prefix = 0;
        return 1;
    }
    if (snprintf(prefix, cap, "%s/", p) >= (int)cap) return 0;
    size_t i = first_under(a, prefix);
    return i < a->n && !strncmp(a->files[i].path, prefix, strlen(prefix));
}

static void verify(Aoc *a, uint32_t i) {
    plat_mutex_lock(&verify_lock);
    AocFile *f = &a->files[i];
    if (f->verified) {
        plat_mutex_unlock(&verify_lock);
        return;
    }
    char p[2400];
    snprintf(p, sizeof p, "%s/%s", a->root, f->path);
    PlatFile *h = plat_file_open(p);
    if (!h) rt_fatal("aoc: add-on file %s disappeared", p);
    Sha256 s;
    sha256_init(&s);
    uint8_t *buf = malloc(8u << 20);
    need(buf != NULL, "hash buffer");
    uint64_t done = 0;
    while (done < f->size) {
        uint32_t want = f->size - done > (8u << 20) ? (8u << 20) : (uint32_t)(f->size - done);
        int64_t got = plat_file_pread(h, buf, want, done);
        if (got <= 0) rt_fatal("aoc: read failure hashing %s", p);
        sha256_update(&s, buf, (size_t)got);
        done += (uint64_t)got;
    }
    free(buf);
    plat_file_close(h);
    uint8_t d[32];
    sha256_final(&s, d);
    if (memcmp(d, f->sha, 32))
        rt_fatal("aoc: add-on %d file %s does not match its manifest SHA-256 (reinstall the DLC)", a->index, p);
    f->verified = 1;
    plat_mutex_unlock(&verify_lock);
}

// ---- files
int aoc_handle(uint64_t h) { return h >= AOC_HANDLE_BASE && h < AOC_HANDLE_BASE + AOC_FILES; }

uint32_t aoc_open(uint64_t *out, const char *path, int mode) {
    fs_trace("aoc", path);
    if (!out || mode != FS_OPEN_READ) return FS_R_UNSUPPORTED_OPEN;
    const char *p;
    int slot = resolve(path, &p);
    if (slot < 0 || !*p) return FS_R_PATH_NOT_FOUND;
    Aoc *a = &aocs[slot];
    long i = file_of(a, p);
    if (i < 0) return FS_R_PATH_NOT_FOUND;
    verify(a, (uint32_t)i);
    char host[2400];
    snprintf(host, sizeof host, "%s/%s", a->root, p);
    PlatFile *f = plat_file_open(host);
    PlatFileId id;
    if (!f || !plat_file_id(f, &id) || id.size != a->files[i].size)
        rt_fatal("aoc: add-on file %s is missing or changed size since the start check", host);
    plat_mutex_lock(&lock);
    for (int j = 0; j < AOC_FILES; j++)
        if (!handles[j].f) {
            handles[j] = (AocHandle){f, (uint32_t)slot, (uint32_t)i};
            *out = AOC_HANDLE_BASE + (uint64_t)j;
            plat_mutex_unlock(&lock);
            return 0;
        }
    plat_mutex_unlock(&lock);
    rt_fatal("aoc: file handle table full (%d)", AOC_FILES);
}

static int handle_get(uint64_t h, AocHandle *out) {
    if (!aoc_handle(h)) return 0;
    plat_mutex_lock_shared(&lock);
    *out = handles[h - AOC_HANDLE_BASE];
    plat_mutex_unlock_shared(&lock);
    return out->f != NULL;
}

uint32_t aoc_read(uint64_t *out, uint64_t h, int64_t offset, void *buffer, uint64_t size) {
    AocHandle e;
    if (!out || offset < 0 || !handle_get(h, &e)) return FS_R_INVALID;
    uint64_t fsize = aocs[e.aoc].files[e.file].size;
    if ((uint64_t)offset > fsize || (!buffer && size)) return FS_R_INVALID;
    uint64_t n = size < fsize - (uint64_t)offset ? size : fsize - (uint64_t)offset, done = 0;
    if (n) mem_thaw((uint64_t)(uintptr_t)buffer, n);
    while (done < n) {
        uint32_t chunk = n - done > AOC_READ_CHUNK ? AOC_READ_CHUNK : (uint32_t)(n - done);
        int64_t r = plat_file_pread(e.f, (uint8_t *)buffer + done, chunk, (uint64_t)offset + done);
        if (r <= 0) rt_fatal("aoc: read failure in %s", aocs[e.aoc].files[e.file].path);
        done += (uint64_t)r;
    }
    *out = done;
    return 0;
}

uint32_t aoc_size(int64_t *out, uint64_t h) {
    AocHandle e;
    if (!out || !handle_get(h, &e)) return FS_R_INVALID;
    *out = (int64_t)aocs[e.aoc].files[e.file].size;
    return 0;
}

void aoc_close(uint64_t h) {
    AocHandle e;
    if (!handle_get(h, &e)) rt_fatal("aoc: close of invalid file handle %#llx", (unsigned long long)h);
    plat_mutex_lock(&lock);
    handles[h - AOC_HANDLE_BASE] = (AocHandle){0};
    plat_mutex_unlock(&lock);
    plat_file_close(e.f);
}

uint32_t aoc_entry_type(int *out, const char *path) {
    const char *p;
    int slot = resolve(path, &p);
    if (!out) return FS_R_INVALID;
    if (slot < 0) return FS_R_PATH_NOT_FOUND;
    char prefix[2100];
    if (*p && file_of(&aocs[slot], p) >= 0) *out = FS_ENTRY_FILE;
    else if (is_dir(&aocs[slot], p, prefix, sizeof prefix)) *out = FS_ENTRY_DIRECTORY;
    else return FS_R_PATH_NOT_FOUND;
    return 0;
}

// ---- directories: the manifest's immediate children of the directory, files and folders
int aoc_dir_handle(uint64_t h) { return h >= AOC_DIR_HANDLE_BASE && h < AOC_DIR_HANDLE_BASE + AOC_DIRS; }

static void listed_free(AocListed *e, int n) {
    for (int i = 0; i < n; i++) free(e[i].name);
    free(e);
}

uint32_t aoc_open_directory(uint64_t *out, const char *path, int mode) {
    const char *p;
    int slot = resolve(path, &p);
    if (!out || mode < 1 || mode > 3) return FS_R_INVALID_ARGUMENT;
    if (slot < 0) return FS_R_PATH_NOT_FOUND;
    const Aoc *a = &aocs[slot];
    char prefix[2100];
    if (!is_dir(a, p, prefix, sizeof prefix)) return FS_R_PATH_NOT_FOUND;
    size_t k = strlen(prefix);
    AocListed *e = NULL;
    int n = 0, cap = 0;
    for (size_t i = first_under(a, prefix); i < a->n && !strncmp(a->files[i].path, prefix, k); i++) {
        const char *rest = a->files[i].path + k;
        size_t len = strcspn(rest, "/");
        int sub = rest[len] == '/';
        // Rows under one folder are contiguous (sorted, shared prefix), so a repeated folder is the previous entry.
        if (n && e[n - 1].is_dir && sub && strlen(e[n - 1].name) == len && !strncmp(e[n - 1].name, rest, len))
            continue;
        if (n == cap) {
            cap = cap ? cap * 2 : 32;
            AocListed *grown = realloc(e, (size_t)cap * sizeof *e);
            need(grown != NULL, "directory listing allocation");
            e = grown;
        }
        char *name = malloc(len + 1);
        need(name != NULL, "directory listing allocation");
        memcpy(name, rest, len);
        name[len] = 0;
        e[n++] = (AocListed){name, sub, sub ? 0 : a->files[i].size};
    }
    plat_mutex_lock(&lock);
    for (int i = 0; i < AOC_DIRS; i++)
        if (!dirs[i].used) {
            dirs[i] = (AocDir){e, n, 0, mode, 1};
            *out = AOC_DIR_HANDLE_BASE + (uint64_t)i;
            plat_mutex_unlock(&lock);
            return 0;
        }
    plat_mutex_unlock(&lock);
    rt_fatal("aoc: directory handle table full (%d)", AOC_DIRS);
}

typedef struct { char name[769]; uint8_t pad0[3]; int8_t type; uint8_t pad1[3]; int64_t size; } AocDirectoryEntry;
_Static_assert(sizeof(AocDirectoryEntry) == 0x310, "nn::fs::DirectoryEntry");

uint32_t aoc_read_directory(int64_t *count, void *out_entries, uint64_t h, int64_t max) {
    AocDirectoryEntry *out = out_entries;
    if (!aoc_dir_handle(h) || !count || max < 0 || (!out && max)) return FS_R_INVALID_ARGUMENT;
    plat_mutex_lock(&lock);
    AocDir *d = &dirs[h - AOC_DIR_HANDLE_BASE];
    if (!d->used) {
        plat_mutex_unlock(&lock);
        return FS_R_INVALID_ARGUMENT;
    }
    int64_t n = 0;
    while (n < max && d->next < d->n) {
        const AocListed *e = &d->e[d->next++];
        size_t k = strlen(e->name) + 1;
        if (k > 769) continue;
        if (!(d->mode & (e->is_dir ? 1 : 2))) continue;   // OpenDirectoryMode: 1 directories, 2 files
        memset(&out[n], 0, sizeof *out);
        memcpy(out[n].name, e->name, k);
        out[n].type = e->is_dir ? FS_ENTRY_DIRECTORY : FS_ENTRY_FILE;
        out[n].size = (int64_t)e->size;
        n++;
    }
    plat_mutex_unlock(&lock);
    *count = n;
    return 0;
}

void aoc_close_directory(uint64_t h) {
    if (!aoc_dir_handle(h)) rt_fatal("aoc: close of invalid directory handle %#llx", (unsigned long long)h);
    plat_mutex_lock(&lock);
    AocDir d = dirs[h - AOC_DIR_HANDLE_BASE];
    dirs[h - AOC_DIR_HANDLE_BASE] = (AocDir){0};
    plat_mutex_unlock(&lock);
    if (!d.used) rt_fatal("aoc: close of invalid directory handle %#llx", (unsigned long long)h);
    listed_free(d.e, d.n);
}

#ifndef AOC_NO_OVERRIDES
// int nn::aoc::CountAddOnContent()
static void ov_aoc_count(Cpu *c) { ret_x(c, (uint64_t)(uint32_t)aoc_count()); }
// int nn::aoc::ListAddOnContent(AddOnContentIndex *out, int offset, int count)
static void ov_aoc_list(Cpu *c) {
    ret_x(c, (uint64_t)(uint32_t)aoc_list(arg_p(c, 0), (int)arg_x(c, 1), (int)arg_x(c, 2)));
}
// nn::Result nn::fs::QueryMountAddOnContentCacheSize(size_t *out, AddOnContentIndex index): the host reads the
// add-on files directly, so no cache is needed (like QueryMountRomCacheSize).
static void ov_query_aoc_cache(Cpu *c) {
    uint64_t *out = arg_p(c, 0);
    ready();
    if (!out) {
        ret_x(c, FS_R_INVALID_ARGUMENT);
        return;
    }
    if (!aoc_by_index((int)arg_x(c, 1))) {
        ret_x(c, FS_R_TARGET_NOT_FOUND);
        return;
    }
    *out = 0;
    ret_x(c, 0);
}
// nn::Result nn::fs::MountAddOnContent(const char *name, AddOnContentIndex index, void *cache, size_t cache_size)
static void ov_mount_aoc(Cpu *c) { ret_x(c, aoc_mount(arg_p(c, 0), (int)arg_x(c, 1))); }

HOST_OVERRIDE("_ZN2nn3aoc17CountAddOnContentEv", ov_aoc_count);
HOST_OVERRIDE("_ZN2nn3aoc16ListAddOnContentEPiii", ov_aoc_list);
HOST_OVERRIDE("_ZN2nn2fs31QueryMountAddOnContentCacheSizeEPmi", ov_query_aoc_cache);
HOST_OVERRIDE("_ZN2nn2fs17MountAddOnContentEPKciPvm", ov_mount_aoc);
#endif
