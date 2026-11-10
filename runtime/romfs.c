// Adapted from MacNativeGames games/totk/runtime/romfs.c @ b75be8cf (MIT, Copyright (c) 2026 MacNativeGames
// contributors).
// Read-only game RomFS backed by the player's extracted image.
//
// Input: <romfs>/romfs.bin (rt_config()->romfs), whose six IVFC levels and SHA-256 tools/extract.py verified, and
// romfs-index.bin next to totk.exe (TOTK_ROMFS_INDEX overrides), which tools/romfs_index.py wrote together with
// romfs_config.h. fs_init checks the index's SHA-256 and layout and the image size. The full 17 GB hash runs only with
// TOTK_ROMFS_VERIFY=full; a proof file (.romfs-verified-v1 next to the index, or in TOTK_ROMFS_PROOF_DIR) keyed by
// the file's volume, file id, size and times skips it on later starts, TOTK_ROMFS_VERIFY=force ignores the proof.
//
// Reads are positional (platform.h plat_file_pread), so threads read the image in parallel without a file-pointer
// lock. TOTK_ROMFS_MMAP=1 maps the image instead.
//
// Overlays: files listed in a directory's manifest.tsv are served in place of the originals. Each line names a RomFS
// path, the overlay's size and SHA-256 and the original's SHA-256 ("-" adds a file the RomFS lacks; it is numbered
// after the RomFS's own files, file_count + k in path order). The directories, in this order: TOTK_ROMFS_PATCHES and
// TOTK_ROMFS_MODS; unset or "0" = none. A missing path, a size or hash mismatch or a changed original stops the start.
// An overlay file is opened when the game opens it; its identity (volume, file id, size, last write) is kept from the
// start check, and an open that finds another file there stops the game.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "fs.h"
#include "host.h"
#include "romfs_config.h"
#include "romfs_path.h"
#include "platform/platform.h"
#include "sha256.h"
#include "romfs_storage.h"

#define NH 65536                     // RomFS handle slots
#define READ_CHUNK (64u << 20)       // largest single read
#define HASH_CHUNK (8u << 20)
#define ADDED UINT64_MAX             // an added overlay file's index until overlay_init numbers it

typedef struct { uint64_t offset, size; uint32_t name, len; } Entry;
_Static_assert(sizeof(Entry) == 24, "index entry layout");

typedef struct { uint32_t volume, pad; uint64_t file_id, size, write_time, create_time; } Identity;   // no padding
typedef struct { uint64_t index, size; char *path, *name; Identity id; } Overlay;

static uint8_t *index_bytes;
static size_t index_size;
static uint64_t file_count, strings_offset;
static PlatFile *image;
#ifdef ROMFS_NUP_MODE
static PlatFile *cache_image;
static uint8_t *entry_hashes, *entry_verified;
#endif
static const uint8_t *image_map;
static char image_file[2048];
static char index_dir[2048];         // the directory of romfs-index.bin: the proof file's default place
static char mount_name[32];
static PlatMutex lock = PLAT_MUTEX_INIT;
static uint64_t handles[NH];         // file index + 1; 0 is free
static uint32_t handle_ovl[NH];      // overlay slot + 1 of an open handle, 0 = original
static PlatFile *handle_file[NH];    // the handle's open overlay file, when handle_ovl is set
static Overlay *overlays;            // sorted by RomFS index after overlay_init
static size_t noverlay, overlay_cap;
static char **added;                 // RomFS paths of the added files, sorted; added[k] is index file_count + k
static size_t nadded;

static void need(int c, const char *m) {
    if (!c) rt_fatal("RomFS: %s", m);
}

static Entry entry(uint64_t i) {
    Entry e;
    memcpy(&e, index_bytes + 24 + i * 24, 24);
    return e;
}
static const char *entry_name(Entry e) { return (const char *)index_bytes + strings_offset + e.name; }

static double now_s(void) { return (double)plat_now_ns() / 1e9; }

// ---- positional reads
// Bytes read at off (0 at or past the end of the file), -1 on an I/O error.
static int64_t read_at(PlatFile *f, void *buf, uint32_t n, uint64_t off) {
#ifdef ROMFS_NUP_MODE
    if (f == image) {
        int cache;
        uint32_t amount;
        uint64_t physical;
        if (!romfs_storage_route(ROMFS_NUP_SIZE, ROMFS_CACHE_SIZE, off, n, &cache, &physical, &amount)) return -1;
        if (cache) f = cache_image;
        off = physical;
        n = amount;
    }
#endif
    mem_thaw((uintptr_t)buf, n);   // the kernel's write would fail on a frozen page instead of faulting
    int64_t got = plat_file_pread(f, buf, n, off);
    if (got == -2)   // a read-only page in the buffer: a frozen region missed its thaw
        rt_log("RomFS: read of %u bytes into %p refused: the buffer is not writable", n, buf);
    return got < 0 ? -1 : got;
}

static PlatFile *open_read(const char *path) { return plat_file_open(path); }

static Identity identity_of(PlatFile *f) {
    PlatFileId i;
    need(plat_file_id(f, &i), "file information");
    return (Identity){(uint32_t)i.volume, 0, i.file_id, i.size, i.write_time, i.create_time};
}

// ---- SHA-256 over a file range, streaming
static void hex32(const uint8_t d[32], char out[65]) {
    for (int i = 0; i < 32; i++) snprintf(out + 2 * i, 3, "%02x", d[i]);
}

static void sha_range(PlatFile *f, uint64_t offset, uint64_t size, char hex[65], int progress) {
    Sha256 h;
    sha256_init(&h);
    uint8_t *buf = malloc(HASH_CHUNK);
    need(buf != NULL, "hash buffer");
    double t0 = now_s();
    for (uint64_t done = 0; done < size;) {
        uint32_t want = size - done > HASH_CHUNK ? HASH_CHUNK : (uint32_t)(size - done);
        int64_t n = read_at(f, buf, want, offset + done);
        need(n > 0, "read of hash input");
        sha256_update(&h, buf, (size_t)n);
        done += (uint64_t)n;
        if (progress && (done % (2ull << 30) < (uint64_t)n || done == size))
            rt_log("RomFS: hashed %.1f of %.1f GiB (%.0f MiB/s)", done / 1073741824.0, size / 1073741824.0,
                   done / 1048576.0 / (now_s() - t0 + 1e-9));
    }
    uint8_t d[32];
    sha256_final(&h, d);
    free(buf);
    hex32(d, hex);
}

// ---- full image verification with a proof file
typedef struct { char magic[16], sha256[65]; uint8_t pad[7]; Identity id; } RomfsProof;

static void verify_image(const char *dir) {
    const char *mode = cfg_get("TOTK_ROMFS_VERIFY");
    if (!mode || (strcmp(mode, "full") && strcmp(mode, "force"))) return;
    const char *pdir = cfg_get("TOTK_ROMFS_PROOF_DIR");
    if (pdir && *pdir) dir = pdir;
    RomfsProof want = {0}, saved;
    memcpy(want.magic, "TOTKRFS-PROOF-1", 16);
    memcpy(want.sha256, ROMFS_IMAGE_SHA256, sizeof ROMFS_IMAGE_SHA256 < 65 ? sizeof ROMFS_IMAGE_SHA256 : 65);
    want.id = identity_of(image);
    char path[2100];
    need(snprintf(path, sizeof path, "%s/.romfs-verified-v1", dir) < (int)sizeof path, "proof path");
    if (strcmp(mode, "force")) {
        PlatFile *p = open_read(path);
        if (p) {
            int ok = plat_file_pread(p, &saved, sizeof saved, 0) == sizeof saved;
            plat_file_close(p);
            if (ok && !memcmp(&saved, &want, sizeof want)) {
                rt_log("RomFS unchanged since its full SHA-256 check (%s)", path);
                return;
            }
        }
    }
    rt_log("RomFS: verifying the SHA-256 of %s (%llu bytes)", image_file, (unsigned long long)want.id.size);
    char hex[65];
    sha_range(image, 0, want.id.size, hex, 1);
    if (strcmp(hex, ROMFS_IMAGE_SHA256))
        rt_fatal("RomFS image %s has SHA-256 %s, the extraction recorded %s (extract it again)", image_file, hex,
                 ROMFS_IMAGE_SHA256);
    Identity after = identity_of(image);
    need(!memcmp(&after, &want.id, sizeof after), "image changed during verification");
    plat_file_write_atomic(path, &want, sizeof want);
    rt_log("RomFS: image SHA-256 ok");
}

#ifdef ROMFS_NUP_MODE
// ---- per-entry verification, remembered across runs
// Every entry is hashed in full before its first open. A run used to start from nothing and hold one lock while it
// hashed, so a scene's loader threads queued behind each other's multi-hundred-MiB files on every launch (HWAoI black
// screens between scenes). Now entries are verified under one of VERIFY_STRIPES locks, and romfs-verified.bin beside
// the index remembers which entries passed for this exact NUP, cache and hash table (identities as in the image proof
// above); a changed NUP, cache or build starts over. TOTK_ROMFS_VERIFY=force ignores the record.
#define VERIFY_STRIPES 64
static PlatMutex verify_stripes[VERIFY_STRIPES];
static char verified_file[2100];
static volatile int64_t verify_ns, verify_bytes, verify_count;
typedef struct {
    char magic[16];
    uint8_t hashes_sha256[32];
    Identity nup, cache;
    uint64_t count;
} VerifiedHeader;   // followed by count bytes, 1 = verified
_Static_assert(sizeof(VerifiedHeader) == 16 + 32 + 2 * sizeof(Identity) + 8, "verified record layout");

static void verified_header(VerifiedHeader *h) {
    memset(h, 0, sizeof *h);
    memcpy(h->magic, "HWAOI-RFSV-1", 12);
    for (int i = 0; i < 32; i++) {
        unsigned v;
        sscanf(&ROMFS_HASHES_SHA256[2 * i], "%2x", &v);
        h->hashes_sha256[i] = (uint8_t)v;
    }
    h->nup = identity_of(image);
    h->cache = identity_of(cache_image);
    h->count = file_count;
}

static void verified_load(void) {
    need(snprintf(verified_file, sizeof verified_file, "%s/romfs-verified.bin", index_dir) < (int)sizeof verified_file,
         "verification record path");
    const char *mode = cfg_get("TOTK_ROMFS_VERIFY");
    if (mode && !strcmp(mode, "force")) return;
    uint64_t size = 0;
    uint8_t *data = plat_file_read(verified_file, &size);
    if (!data) return;
    VerifiedHeader want;
    verified_header(&want);
    uint64_t n = 0;
    if (size == sizeof want + file_count && !memcmp(data, &want, sizeof want)) {
        for (uint64_t i = 0; i < file_count; i++) n += entry_verified[i] = data[sizeof want + i] == 1;
        rt_log("RomFS: %llu of %llu entries already verified for this NUP (%s)", (unsigned long long)n,
               (unsigned long long)file_count, verified_file);
    } else {
        rt_log("RomFS: %s is for another NUP, cache or build; entries are verified again", verified_file);
    }
    free(data);
}

static void verified_save(void) {   // under no stripe lock; writes are whole-file and atomic
    static PlatMutex save_lock = PLAT_MUTEX_INIT;
    plat_mutex_lock(&save_lock);
    size_t n = sizeof(VerifiedHeader) + (size_t)file_count;
    uint8_t *data = malloc(n);
    if (data) {
        verified_header((VerifiedHeader *)data);
        for (uint64_t i = 0; i < file_count; i++) data[sizeof(VerifiedHeader) + i] = __atomic_load_n(&entry_verified[i], __ATOMIC_ACQUIRE);
        if (!plat_file_write_atomic(verified_file, data, n)) rt_log("RomFS: cannot write %s", verified_file);
        free(data);
    }
    plat_mutex_unlock(&save_lock);
}

static void verify_entry(uint64_t i) {
    need(i < file_count, "hash entry index");
    if (__atomic_load_n(&entry_verified[i], __ATOMIC_ACQUIRE)) return;
    PlatMutex *m = &verify_stripes[i % VERIFY_STRIPES];
    plat_mutex_lock(m);
    int fresh = 0;
    if (!entry_verified[i]) {
        Entry e = entry(i);
        char expected[65], actual[65];
        hex32(entry_hashes + i * 32, expected);
        double t0 = now_s();
        sha_range(image, e.offset, e.size, actual, 0);
        if (strcmp(expected, actual)) rt_fatal("RomFS entry %s failed SHA-256", entry_name(e));
        int64_t ns = (int64_t)((now_s() - t0) * 1e9);
        int64_t total_ns = plat_add64(&verify_ns, ns), total = plat_add64(&verify_bytes, (int64_t)e.size);   // new totals
        int64_t count = plat_add64(&verify_count, 1);
        if (ns >= 100000000)
            rt_log("RomFS: first-open verification of %s (%.1f MiB) took %.0f ms; this run %lld entries, %.2f GiB in %.1f s",
                   entry_name(e), e.size / 1048576.0, ns / 1e6, (long long)count, total / 1073741824.0, total_ns / 1e9);
        __atomic_store_n(&entry_verified[i], 1, __ATOMIC_RELEASE);
        fresh = 1;
    }
    plat_mutex_unlock(m);
    if (fresh) verified_save();
}

static void open_nup_storage(const char *dir) {
    const char *override = getenv("HWAOI_NUP_PATH");
    need(snprintf(image_file, sizeof image_file, "%s", override && *override ? override : ROMFS_NUP_PATH) <
         (int)sizeof image_file, "NUP path");
    image = open_read(image_file);
    need(image != NULL, "source NUP missing");
    need(identity_of(image).size == ROMFS_NUP_SIZE, "source NUP size changed");
    char path[2100], digest[65];
    need(snprintf(path, sizeof path, "%s/cache.bin", dir) < (int)sizeof path, "cache path");
    cache_image = open_read(path);
    need(cache_image != NULL && identity_of(cache_image).size == ROMFS_CACHE_SIZE, "RomFS cache missing or truncated");
    sha_range(image, 0, 0x80, digest, 0);
    need(!strcmp(digest, ROMFS_NUP_HEADER_SHA256), "source NUP header SHA-256");
    sha_range(image, ROMFS_NUP_TOC_OFFSET, ROMFS_NUP_TOC_SIZE, digest, 0);
    need(!strcmp(digest, ROMFS_NUP_TOC_SHA256), "source NUP TOC SHA-256");
    need(snprintf(path, sizeof path, "%s/romfs-hashes.bin", index_dir) < (int)sizeof path, "entry hash path");
    uint64_t bytes;
    entry_hashes = plat_file_read(path, &bytes);
    need(entry_hashes && bytes == file_count * 32, "entry hash table size");
    uint8_t hash[32];
    rt_sha256(entry_hashes, (size_t)bytes, hash);
    hex32(hash, digest);
    need(!strcmp(digest, ROMFS_HASHES_SHA256), "entry hash table SHA-256");
    entry_verified = calloc(file_count ? (size_t)file_count : 1, 1);
    need(entry_verified != NULL, "entry verification allocation");
    verified_load();
    const char *mode = cfg_get("TOTK_ROMFS_VERIFY");
    if (mode && (!strcmp(mode, "full") || !strcmp(mode, "force"))) {
        sha_range(cache_image, 0, ROMFS_CACHE_SIZE, digest, 1);
        need(!strcmp(digest, ROMFS_CACHE_SHA256), "cache SHA-256");
        for (uint64_t i = 0; i < file_count; i++) verify_entry(i);
    }
    rt_log("RomFS NUP/cache mounted: %llu files; payload SHA-256 checked on first open; no full-image copy",
           (unsigned long long)file_count);
}
#endif

// ---- index lookup
static uint64_t lower(const char *name) {
    uint64_t lo = 0, hi = file_count;
    while (lo < hi) {
        uint64_t m = lo + (hi - lo) / 2;
        if (strcmp(entry_name(entry(m)), name) < 0) lo = m + 1;
        else hi = m;
    }
    return lo;
}

static size_t added_lower(const char *p) {
    size_t lo = 0, hi = nadded;
    while (lo < hi) {
        size_t m = lo + (hi - lo) / 2;
        if (strcmp(added[m], p) < 0) lo = m + 1;
        else hi = m;
    }
    return lo;
}

// The index of RomFS path p (an added file: file_count + k), or UINT64_MAX.
static uint64_t file_index(const char *p) {
    uint64_t i = lower(p);
    if (i < file_count && !strcmp(p, entry_name(entry(i)))) return i;
    size_t k = added_lower(p);
    return k < nadded && !strcmp(added[k], p) ? file_count + k : UINT64_MAX;
}

// 1 when a RomFS file or an added one lies under folder (a path ending in '/').
static int folder_exists(const char *folder) {
    size_t n = strlen(folder);
    uint64_t i = lower(folder);
    if (i < file_count && !strncmp(entry_name(entry(i)), folder, n)) return 1;
    size_t k = added_lower(folder);
    return k < nadded && !strncmp(added[k], folder, n);
}

// The overlay slot + 1 that replaces RomFS file i, or 0.
static uint32_t overlay_slot(uint64_t i) {
    size_t lo = 0, hi = noverlay;
    while (lo < hi) {
        size_t m = lo + (hi - lo) / 2;
        if (overlays[m].index < i) lo = m + 1;
        else hi = m;
    }
    return lo < noverlay && overlays[lo].index == i ? (uint32_t)(lo + 1) : 0;
}

// ---- overlays
// Loads and checks one overlay folder. A failed check stops the game, except with optional set (the Xbox prompts
// folder): the folder is dropped, the reason logged, and 0 returned. Returns 1 when the folder is loaded or has no
// manifest.
static int overlay_load(const char *dir, int optional) {
#define BAD(...) do { if (!optional) rt_fatal(__VA_ARGS__); rt_log(__VA_ARGS__); goto bad; } while (0)
    char path[2100];
    need(snprintf(path, sizeof path, "%s/manifest.tsv", dir) < (int)sizeof path, "overlay path");
    FILE *m = fopen(path, "rb");
    if (!m) {
        rt_log("RomFS overlay: none (%s)", path);
        return 1;
    }
    char line[8192];
    size_t first = noverlay;
    uint64_t hashed = 0;
    double t0 = now_s();
    while (fgets(line, sizeof line, m)) {
        if (line[0] == '#' || line[0] == '\n' || line[0] == '\r') continue;
        char name[4096], sha[65], orig[65], hex[65];
        unsigned long long size = 0;
        need(sscanf(line, "%4095[^\t]\t%llu\t%64[0-9a-f]\t%64[0-9a-f-]", name, &size, sha, orig) == 4 &&
             strlen(sha) == 64 && (strlen(orig) == 64 || !strcmp(orig, "-")), "overlay manifest line");
        need(romfs_path_valid_relative(name), "overlay path");
        uint64_t i = lower(name);
        int known = i < file_count && !strcmp(entry_name(entry(i)), name), add = !strcmp(orig, "-");
        if (!known && !add)
            BAD("RomFS overlay: %s/manifest.tsv names %s, which the RomFS lacks; a new file has \"-\" as its "
                "original's SHA-256", dir, name);
        if (known && add)
            BAD("RomFS overlay: %s/manifest.tsv adds %s, which the RomFS already has; give its original's SHA-256",
                dir, name);
        Entry e = {0};
        if (known) {
            e = entry(i);
            sha_range(image, e.offset, e.size, hex, 0);
            if (strcmp(hex, orig)) BAD("RomFS overlay: the original of %s has SHA-256 %s, %s/manifest.tsv says %s",
                                       name, hex, dir, orig);
        } else {
            i = ADDED;
        }
        char file[8200];
        need(snprintf(file, sizeof file, "%s/%s", dir, name) < (int)sizeof file, "overlay file path");
        PlatFile *f = open_read(file);
        if (!f) BAD("RomFS overlay: %s missing", file);
        Identity id = identity_of(f);
        if (id.size != size) {
            plat_file_close(f);
            BAD("RomFS overlay: %s has %llu bytes, its manifest says %llu", file, (unsigned long long)id.size, size);
        }
        sha_range(f, 0, size, hex, 0);
        plat_file_close(f);
        if (strcmp(hex, sha)) BAD("RomFS overlay: %s has SHA-256 %s, its manifest says %s", file, hex, sha);
        hashed += e.size + size;
        if (noverlay == overlay_cap) {
            overlay_cap = overlay_cap ? overlay_cap * 2 : 16;
            overlays = realloc(overlays, overlay_cap * sizeof *overlays);
            need(overlays != NULL, "overlay table");
        }
        char *copy = strdup(file), *rel = strdup(name);
        need(copy && rel, "overlay path copy");
        overlays[noverlay++] = (Overlay){i, size, copy, rel, id};
        if (noverlay - first <= 16)
            rt_log("RomFS overlay: %s (%llu bytes) %s", name, size, add ? "is added" : "replaces the original");
    }
    fclose(m);
    if (noverlay - first > 16) rt_log("RomFS overlay: %zu files from %s (%zu not listed)", noverlay - first, dir,
                                      noverlay - first - 16);
    if (noverlay > first) rt_log("RomFS overlay: %s checked, %zu files, %.1f MiB hashed in %.2f s", dir,
                                 noverlay - first, hashed / 1048576.0, now_s() - t0);
    return 1;
bad:
    fclose(m);
    for (size_t k = first; k < noverlay; k++) {
        free((void *)overlays[k].path);
        free((void *)overlays[k].name);
    }
    noverlay = first;
    return 0;
#undef BAD
}

static int overlay_order(const void *a, const void *b) {
    uint64_t x = ((const Overlay *)a)->index, y = ((const Overlay *)b)->index;
    return (x > y) - (x < y);
}

static int added_order(const void *a, const void *b) {
    return strcmp((*(Overlay *const *)a)->name, (*(Overlay *const *)b)->name);
}

// Number the added files in path order after the RomFS's own (file_count + k); two overlays adding one path stop.
static void added_init(void) {
    Overlay **list = NULL;
    for (size_t k = 0; k < noverlay; k++)
        if (overlays[k].index == ADDED) {
            list = realloc(list, (nadded + 1) * sizeof *list);
            need(list != NULL, "added files");
            list[nadded++] = &overlays[k];
        }
    if (!nadded) return;
    qsort(list, nadded, sizeof *list, added_order);
    added = malloc(nadded * sizeof *added);
    need(added != NULL, "added files");
    for (size_t k = 0; k < nadded; k++) {
        if (k && !strcmp(list[k]->name, list[k - 1]->name))
            rt_fatal("RomFS overlay: %s and %s both add %s", list[k - 1]->path, list[k]->path, list[k]->name);
        list[k]->index = file_count + k;
        added[k] = list[k]->name;
    }
    free(list);
    rt_log("RomFS overlay: %zu files added to the RomFS", nadded);
}

// The HeapDef table a bigger game resolution needs (resolution.c): its texture heap and that heap's parent grow. The
// base is an overlay that replaces the table (a mod's heap sizes stay), else the RomFS original; the grown table is
// written to <profile>/resolution/ and served in the base's place.
#define HEAPDEF "System/HeapSize/Product.Nin_NX_NVN.HeapDef.byml.zs"
static void heapdef_overlay(void) {
    if (!res_heap_extra_kib()) return;
    uint64_t i = lower(HEAPDEF);
    if (i >= file_count || strcmp(entry_name(entry(i)), HEAPDEF)) {
        rt_log("RomFS: %s missing; the game keeps its own resolution", HEAPDEF);
        return;
    }
    Overlay *base = NULL;
    for (size_t k = 0; k < noverlay; k++)
        if (overlays[k].index == i) base = &overlays[k];
    PlatFile *f = base ? open_read(base->path) : image;
    uint64_t off = base ? 0 : entry(i).offset, n = base ? base->size : entry(i).size;
    void *in = n && n < (16u << 20) && f ? malloc((size_t)n) : NULL;
    int ok = in && read_at(f, in, (uint32_t)n, off) == (int64_t)n;
    if (base && f) plat_file_close(f);
    void *out = NULL;
    size_t out_n = 0;
    ok = ok && res_heapdef_build(in, (size_t)n, &out, &out_n);
    free(in);
    if (!ok) return;
    char dir[2100], file[2200];
    need(snprintf(dir, sizeof dir, "%s/resolution", rt_config()->profile) < (int)sizeof dir, "resolution path");
    need(snprintf(file, sizeof file, "%s/Product.Nin_NX_NVN.HeapDef.byml.zs", dir) < (int)sizeof file, "resolution path");
    plat_file_mkdir_p(dir);
    ok = plat_file_write(file, out, out_n);
    free(out);
    PlatFile *r = ok ? open_read(file) : NULL;
    if (!r) {
        rt_log("RomFS: cannot write %s; the game keeps its own resolution", file);
        return;
    }
    Identity id = identity_of(r);
    plat_file_close(r);
    char *copy = strdup(file);
    need(copy != NULL, "overlay path copy");
    if (base) {
        free(base->path);
        base->path = copy;
        base->size = out_n;
        base->id = id;
    } else {
        if (noverlay == overlay_cap) {
            overlay_cap = overlay_cap ? overlay_cap * 2 : 16;
            overlays = realloc(overlays, overlay_cap * sizeof *overlays);
            need(overlays != NULL, "overlay table");
        }
        char *rel = strdup(HEAPDEF);
        need(rel != NULL, "overlay path copy");
        overlays[noverlay++] = (Overlay){i, out_n, copy, rel, id};
    }
    rt_log("RomFS overlay: %s (%zu bytes, grown heaps) replaces %s", file, out_n, base ? "the mod's table" : "the original");
    res_heapdef_served();
}

static int xbox_prompts_ready;    // romfs-patches-xbox/manifest.tsv was next to romfs-index.bin at start
static int xbox_prompts_active;   // and overlay_init loaded it

int romfs_xbox_prompts_ready(void) { return xbox_prompts_ready; }
int romfs_xbox_prompts_active(void) { return xbox_prompts_active; }

static void overlay_init(void) {
    // TOTK_ROMFS_PATCHES unset: romfs-patches next to romfs-index.bin if it has a manifest (tools/make_romfs_patches.py).
    // A game resolution that is not 16:9 takes romfs-patches-<w>x<h> instead when it is there: the same overlay with
    // the UI laid out for that shape (make_romfs_patches.py --aspect), when the game version has the camera aspect patch
    // the layout goes with (res_wants_wide_overlay).
    // With pad_layout = xbox_prompts, romfs-patches-xbox in its place: the same patches with the A/B and X/Y prompts
    // swapped (make_romfs_patches.py --xbox-prompts). The game reads the archives once at boot, so the prompts stay
    // until restart even though the layout itself changes live. Each folder replaces romfs-patches as a whole and the
    // setup builds no wide folder with Xbox prompts, so a wide game resolution keeps the Switch prompts.
    char def[2100], xbox[2100];
    const char *patches = cfg_get("TOTK_ROMFS_PATCHES");
    int wide = 0;
    unsigned out[2];
    res_wanted(out);
    if (!patches && res_wants_wide_overlay(out)) {
        if (snprintf(def, sizeof def, "%s/romfs-patches-%ux%u/manifest.tsv", index_dir, out[0], out[1]) < (int)sizeof def &&
            plat_file_exists(def)) {
            def[strlen(def) - strlen("/manifest.tsv")] = 0;
            patches = def, wide = 1;
        } else {
            rt_log("RomFS overlay: no romfs-patches-%ux%u for this game resolution, the UI keeps its 16:9 layout "
                   "(tools/make_romfs_patches.py --aspect %ux%u)", out[0], out[1], out[0], out[1]);
        }
    }
    xbox_prompts_ready =
        snprintf(xbox, sizeof xbox, "%s/romfs-patches-xbox/manifest.tsv", index_dir) < (int)sizeof xbox &&
        plat_file_exists(xbox);
    int want_xbox = !strcasecmp(cfg_value("TOTK_PAD_LAYOUT"), "xbox_prompts");
    if (!patches && want_xbox && xbox_prompts_ready) {
        xbox[strlen(xbox) - strlen("/manifest.tsv")] = 0;
        xbox_prompts_active = overlay_load(xbox, 1);
        if (xbox_prompts_active) rt_log("RomFS overlay: Xbox button prompts (%s)", xbox);
        else rt_log("RomFS overlay: %s failed its check (run the setup again); romfs-patches loads instead", xbox);
    } else if (want_xbox) {
        rt_log("RomFS overlay: pad_layout = xbox_prompts, but %s; Switch prompts stay",
               wide      ? "the game resolution takes the wide UI layout"
               : patches ? "TOTK_ROMFS_PATCHES is set"
                         : "romfs-patches-xbox has no manifest.tsv (run the setup again)");
    }
    if (!patches && !xbox_prompts_active && snprintf(def, sizeof def, "%s/romfs-patches/manifest.tsv", index_dir) < (int)sizeof def &&
        plat_file_exists(def)) {
        def[strlen(def) - strlen("/manifest.tsv")] = 0;
        patches = def;
    }
    const char *dirs[2] = {patches, cfg_get("TOTK_ROMFS_MODS")};
    for (int i = 0; i < 2; i++)
        if (dirs[i] && *dirs[i] && strcmp(dirs[i], "0")) overlay_load(dirs[i], 0);
    heapdef_overlay();
    added_init();
    if (noverlay) qsort(overlays, noverlay, sizeof *overlays, overlay_order);
    for (size_t k = 1; k < noverlay; k++)
        if (overlays[k].index == overlays[k - 1].index)
            rt_fatal("RomFS overlay: %s and %s replace the same file %s", overlays[k - 1].path, overlays[k].path,
                     overlays[k].name);
}

// ---- start
// romfs-index.bin next to totk.exe, or TOTK_ROMFS_INDEX.
static void index_path(char *out, size_t cap) {
    const char *e = cfg_get("TOTK_ROMFS_INDEX");
    if (e && *e) {
        need(snprintf(out, cap, "%s", e) < (int)cap, "index path");
        return;
    }
    need(plat_exe_path(out, cap), "executable path");
    char *sep = plat_last_sep(out);
    size_t k = sep ? (size_t)(sep - out) + 1 : 0;
    need(k + 16 < cap, "executable path");
    strcpy(out + k, "romfs-index.bin");
}

static void load_index(void) {
    char path[2100];
    index_path(path, sizeof path);
    snprintf(index_dir, sizeof index_dir, "%s", path);
    char *sep = plat_last_sep(index_dir);
    if (sep) *sep = 0;
    else strcpy(index_dir, ".");
    uint64_t sz = 0;
    index_bytes = plat_file_read(path, &sz);
    if (!index_bytes) rt_fatal("RomFS index %s missing (tools/build_game.py writes it next to totk.exe)", path);
    need(sz >= 24 && sz < (128 << 20), "index size");
    index_size = (size_t)sz;
    uint8_t d[32];
    char hex[65];
    rt_sha256(index_bytes, index_size, d);
    hex32(d, hex);
    if (strcmp(hex, ROMFS_INDEX_SHA256))
        rt_fatal("RomFS index %s has SHA-256 %s, this build expects %s (rebuild with tools/build_game.py)", path, hex,
                 ROMFS_INDEX_SHA256);
    need(!memcmp(index_bytes, "TOTKRFS1", 8), "index format");
    memcpy(&file_count, index_bytes + 8, 8);
    memcpy(&strings_offset, index_bytes + 16, 8);
    need(file_count == ROMFS_FILE_COUNT && file_count < 1000000 && strings_offset == 24 + file_count * 24 &&
         strings_offset < index_size, "index layout");
    const char *previous = NULL;
    for (uint64_t i = 0; i < file_count; i++) {
        Entry e = entry(i);
        need(e.offset <= ROMFS_IMAGE_SIZE && e.size <= ROMFS_IMAGE_SIZE - e.offset, "file bounds");
#ifdef ROMFS_NUP_MODE
        need(e.offset >= ROMFS_NUP_SIZE || e.size <= ROMFS_NUP_SIZE - e.offset, "file crosses storage boundary");
#endif
        need(e.name < index_size - strings_offset && e.len > 1 && e.len <= index_size - strings_offset - e.name,
             "name bounds");
        const char *name = entry_name(e);
        need(name[e.len - 1] == 0 && strlen(name) == e.len - 1 && (!previous || strcmp(previous, name) < 0),
             "sorted paths");
        previous = name;
    }
    rt_log("RomFS index %s: SHA-256 ok, %llu files, %zu bytes", path, (unsigned long long)file_count, index_size);
}

static void init_once(void *param) {
    (void)param;
#ifdef ROMFS_MISSING
    rt_fatal("this build has no RomFS: tools/build_game.py found no index.json in its --romfs directory");
#else
    double t0 = now_s();
    load_index();
    const char *dir = rt_config()->romfs;
#ifdef ROMFS_NUP_MODE
    open_nup_storage(dir);
#else
    need(snprintf(image_file, sizeof image_file, "%s/romfs.bin", dir) < (int)sizeof image_file, "image path");
    image = open_read(image_file);
    if (!image) rt_fatal("RomFS image %s missing; put romfs.bin into the romfs folder next to totk or set TOTK_ROMFS",
                         image_file);
    PlatFileId sz;
    need(plat_file_id(image, &sz), "image size");
    if (sz.size != ROMFS_IMAGE_SIZE)
        rt_fatal("RomFS image %s has %llu bytes, the index was built for %llu", image_file,
                 (unsigned long long)sz.size, (unsigned long long)ROMFS_IMAGE_SIZE);
    rt_log("RomFS image %s: size ok (%llu bytes)", image_file, (unsigned long long)ROMFS_IMAGE_SIZE);
    verify_image(index_dir);
    const char *mm = cfg_get("TOTK_ROMFS_MMAP");
    if (mm && strcmp(mm, "0")) {
        const void *v = plat_file_map(image);
        if (v) {
            image_map = v;
            rt_log("RomFS: mapped read path enabled");
        } else {
            rt_log("RomFS: mapping failed; using positional reads");
        }
    }
#endif
    overlay_init();
    rt_log("RomFS ready: %llu files, %zu overlays, %zu added, %.2f s", (unsigned long long)file_count, noverlay,
           nadded, now_s() - t0);
#endif
    return;
}

void fs_init(void) {
    static PlatOnce once = PLAT_ONCE_INIT;
    plat_once(&once, init_once, NULL);
}

// ---- the RomFS interface (fs.h)
void fs_trace(const char *what, const char *path) {
    static double t0;
    if (!(rt_trace & RT_TRACE_FS)) return;
    if (!t0) t0 = now_s();
    rt_log("fs %8.3f %s %s", now_s() - t0, what, path ? path : "(null)");
}

uint64_t romfs_file_count(void) { return file_count; }

const char *romfs_entry(uint64_t i, uint64_t *offset, uint64_t *size) {
    if (i >= file_count) return NULL;
    Entry e = entry(i);
    if (offset) *offset = e.offset;
    if (size) *size = e.size;
    return entry_name(e);
}

const char *romfs_image_path(void) { return image_file; }

size_t romfs_overlay_count(void) { return noverlay; }

const char *romfs_overlay(size_t k, const char **file, uint64_t *size, int *is_added) {
    if (k >= noverlay) return NULL;
    *file = overlays[k].path;
    *size = overlays[k].size;
    *is_added = overlays[k].index >= file_count;
    return overlays[k].name;
}

int romfs_owns(const char *path) {
    size_t n = strlen(mount_name);
    return n && path && !strncmp(path, mount_name, n) && path[n] == ':';
}

int romfs_handle(uint64_t h) { return h > ROMFS_HANDLE_BASE && h < ROMFS_HANDLE_BASE + NH; }

uint32_t romfs_mount(const char *name) {
    fs_init();
    if (!name || !romfs_path_valid_relative(name) || strlen(name) >= sizeof mount_name || strchr(name, '/') ||
        strchr(name, ':'))
        return FS_R_INVALID;
    plat_mutex_lock(&lock);
    if (*mount_name && strcmp(name, mount_name)) rt_fatal("second RomFS mount %s (%s is mounted)", name, mount_name);
    strcpy(mount_name, name);
    plat_mutex_unlock(&lock);
    rt_log("fs mount %s: -> RomFS", name);
    return 0;
}

// Optional bounded observations of the actual open result. The older path-only
// trace runs before validation, so repeated lines alone do not prove an open
// succeeded (or distinguish demux reads from a rejected-mode retry loop).
static uint32_t romfs_open_result(const char *path, int mode, const uint64_t *out, uint32_t result) {
    static int enabled = -1;
    static uint64_t calls, successes, failures;
    int on = __atomic_load_n(&enabled, __ATOMIC_RELAXED);
    if (on < 0) {
        const char *e = cfg_get("TOTK_FS_DIAG");
        on = e && !strcmp(e, "1");
        __atomic_store_n(&enabled, on, __ATOMIC_RELAXED);
    }
    if (!on) return result;
    uint64_t seq = __atomic_add_fetch(&calls, 1, __ATOMIC_RELAXED);
    uint64_t failed = result ? __atomic_add_fetch(&failures, 1, __ATOMIC_RELAXED) :
                              __atomic_load_n(&failures, __ATOMIC_RELAXED);
    uint64_t ok = result ? __atomic_load_n(&successes, __ATOMIC_RELAXED) :
                          __atomic_add_fetch(&successes, 1, __ATOMIC_RELAXED);
    if (seq <= 32 || (result && failed <= 32) || !(seq % 16384))
        rt_log("fs-diag: RomFS open seq=%llu mode=%#x result=%#x handle=%#llx successes=%llu failures=%llu path=%s",
               (unsigned long long)seq, (unsigned)mode, result,
               (unsigned long long)(!result && out ? *out : 0),
               (unsigned long long)ok, (unsigned long long)failed, path ? path : "(null)");
    return result;
}

uint32_t romfs_open(uint64_t *out, const char *path, int mode) {
    fs_trace("rom", path);
    if (!out || mode != FS_OPEN_READ) return romfs_open_result(path, mode, out, FS_R_UNSUPPORTED_OPEN);
    const char *p = romfs_path_relative(mount_name, path);
    if (!p) return romfs_open_result(path, mode, out, FS_R_PATH_NOT_FOUND);
    uint64_t i = file_index(p);
    if (i == UINT64_MAX) return romfs_open_result(path, mode, out, FS_R_PATH_NOT_FOUND);
    uint32_t ov = overlay_slot(i);
#ifdef ROMFS_NUP_MODE
    if (!ov) verify_entry(i);
#endif
    PlatFile *f = NULL;
    if (ov) {
        const Overlay *o = &overlays[ov - 1];
        f = open_read(o->path);
        Identity id;
        if (f) id = identity_of(f);
        if (!f || memcmp(&id, &o->id, sizeof id))
            rt_fatal("RomFS overlay %s is missing or changed since the start check (restart the game)", o->path);
    }
    plat_mutex_lock(&lock);
    for (unsigned j = 1; j < NH; j++)
        if (!handles[j]) {
            handles[j] = i + 1;
            handle_ovl[j] = ov;
            handle_file[j] = f;
            *out = ROMFS_HANDLE_BASE + j;
            plat_mutex_unlock(&lock);
            return romfs_open_result(path, mode, out, 0);
        }
    plat_mutex_unlock(&lock);
    rt_fatal("RomFS file handle table full (%d)", NH);
}

// The handle's index row (an added file's is empty: its bytes are all in its overlay), and where its bytes come from:
// the overlay file, or the original's range of the image.
static int handle_source(uint64_t h, PlatFile **f, uint64_t *base, uint64_t *size) {
    if (!romfs_handle(h)) return 0;
    uint64_t j = h - ROMFS_HANDLE_BASE;
    plat_mutex_lock_shared(&lock);
    uint64_t i = handles[j];
    uint32_t ov = handle_ovl[j];
    PlatFile *of = handle_file[j];
    plat_mutex_unlock_shared(&lock);
    if (!i) return 0;
    if (ov) {
        *f = of;
        *base = 0;
        *size = overlays[ov - 1].size;
    } else {
        Entry e = entry(i - 1);
        *f = image;
        *base = e.offset;
        *size = e.size;
    }
    return 1;
}

uint32_t romfs_read(uint64_t *out, uint64_t h, int64_t offset, void *buffer, uint64_t size) {
    PlatFile *f;
    uint64_t base, fsize;
    if (!out || offset < 0 || !handle_source(h, &f, &base, &fsize)) return FS_R_INVALID;
    if ((uint64_t)offset > fsize || (!buffer && size)) return FS_R_INVALID;
    uint64_t n = size < fsize - (uint64_t)offset ? size : fsize - (uint64_t)offset, done = 0;
    if (f == image && image_map) {
        memcpy(buffer, image_map + base + (uint64_t)offset, (size_t)n);
        done = n;
    }
    while (done < n) {
        uint32_t chunk = n - done > READ_CHUNK ? READ_CHUNK : (uint32_t)(n - done);
        int64_t r = read_at(f, (uint8_t *)buffer + done, chunk, base + (uint64_t)offset + done);
        if (r <= 0) rt_fatal("RomFS read failure at %#llx", (unsigned long long)(base + offset + done));
        done += (uint64_t)r;
    }
    *out = done;
    return 0;
}

uint32_t romfs_size(int64_t *out, uint64_t h) {
    PlatFile *f;
    uint64_t base, size;
    if (!out || !handle_source(h, &f, &base, &size)) return FS_R_INVALID;
    *out = (int64_t)size;
    return 0;
}

void romfs_close(uint64_t h) {
    PlatFile *f;
    uint64_t base, size;
    if (!handle_source(h, &f, &base, &size))
        rt_fatal("RomFS: close of invalid file handle %#llx", (unsigned long long)h);
    uint64_t j = h - ROMFS_HANDLE_BASE;
    plat_mutex_lock(&lock);
    PlatFile *of = handle_ovl[j] ? handle_file[j] : NULL;
    handles[j] = 0;
    handle_ovl[j] = 0;
    handle_file[j] = NULL;
    plat_mutex_unlock(&lock);
    if (of) plat_file_close(of);
}

uint32_t romfs_entry_type(int *out, const char *path) {
    if (!out) return FS_R_INVALID;
    const char *p = romfs_path_relative(mount_name, path);
    if (!p) return FS_R_PATH_NOT_FOUND;
    if (file_index(p) != UINT64_MAX) {
        *out = FS_ENTRY_FILE;
        return 0;
    }
    char dir[4096];
    if (snprintf(dir, sizeof dir, "%s/", p) >= (int)sizeof dir) return FS_R_INVALID;
    if (folder_exists(dir)) {
        *out = FS_ENTRY_DIRECTORY;
        return 0;
    }
    return FS_R_PATH_NOT_FOUND;
}

// ---- overrides
// nn::Result nn::fs::MountRom(const char *name, void *cache, size_t cache_size)
static void ov_mount_rom(Cpu *c) { ret_x(c, romfs_mount(arg_p(c, 0))); }
// nn::Result nn::fs::QueryMountRomCacheSize(size_t *out): no cache needed, the host reads the image directly
static void ov_query_cache(Cpu *c) {
    uint64_t *out = arg_p(c, 0);
    if (out) *out = 0;
    ret_x(c, out ? 0 : FS_R_INVALID);
}
// void nn::fs::InitializeForApplication(): SDK-internal; opens the index instead of the fs service session
static void ov_initialize(Cpu *c) {
    (void)c;
    fs_init();
}

HOST_OVERRIDE("_ZN2nn2fs8MountRomEPKcPvm", ov_mount_rom);
HOST_OVERRIDE("_ZN2nn2fs22QueryMountRomCacheSizeEPm", ov_query_cache);
HOST_OVERRIDE("_ZN2nn2fs24InitializeForApplicationEv", ov_initialize);

// This host filesystem does not enable Horizon's global access-log facility.
// Keep the SDK SetAllocator body running so its callbacks are installed, but
// answer its logging-policy query locally instead of opening an unused fsp-srv.
// Host-side HWAOI_TRACE=fs logging is independent and remains available.
static void ov_access_log_enabled(Cpu *c) { ret_x(c, 0); }
HOST_OVERRIDE("_ZN2nn2fs6detail18IsEnabledAccessLogEj", ov_access_log_enabled);
