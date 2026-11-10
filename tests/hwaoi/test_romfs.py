"""Synthetic NUP/cache/index tests, with a native storage-routing check."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests/prototype"))
from test_formats import make_nup, refresh_toc_hash, TITLE_ID
from hwaoi_romfs import prepare, romfs_inventory, select_romfs, verified_application
from hwrecomp.nup import Nup
import romfs_index


class RomfsTests(unittest.TestCase):
    def fixture(self, directory, *, kind=0, title=TITLE_ID, version=0):
        payloads = {"raw.bin": b"raw fixture bytes", "z.bin": b"compressed fixture" * 1024}
        raw, offsets = make_nup([{"kind": kind, "title_id": title, "version": version, "flags": int(kind == 1),
                                  "files": [(n, d, int(n == "z.bin")) for n, d in payloads.items()]}])
        struct.pack_into("<I", raw, 0x80 + 0x30 + 0x10, 1)  # synthetic tree also serves as RomFS
        refresh_toc_hash(raw)
        path = directory / "fixture.nup"
        path.write_bytes(raw)
        expected = {"title_id": f"{TITLE_ID:016X}", "ncm_version": version,
                    "exefs_sha256": {n: hashlib.sha256(d).hexdigest() for n, d in payloads.items()}}
        with Nup(path) as archive:
            expected["romfs_inventory"] = romfs_inventory(select_romfs(archive, archive.packages))
        return path, expected, payloads

    def test_complete_standalone_update_prepares_and_keeps_source_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path, expected, payloads = self.fixture(folder, kind=1, title=TITLE_ID | 0x800, version=10)
            original = path.read_bytes()
            result = prepare(path, folder / "romfs", expected, reserve_bytes=0)
            self.assertEqual(result["files"], len(payloads))
            self.assertEqual(path.read_bytes(), original)
            manifest = json.loads((folder / "romfs/index.json").read_text())
            self.assertEqual(manifest["selected_packages"], [0])
            self.assertEqual(manifest["romfs_inventory"], expected["romfs_inventory"])

    def test_inventory_is_independent_of_offsets_compression_and_iteration_order(self):
        with tempfile.TemporaryDirectory() as temp:
            path, expected, _ = self.fixture(Path(temp))
            with Nup(path) as archive:
                files = select_romfs(archive, archive.packages)
                repacked = [(name, replace(member, offset=0x100000, stored_size=17, codec=1)) for name, member in reversed(files)]
                self.assertEqual(romfs_inventory(repacked), expected["romfs_inventory"])
                self.assertNotEqual(romfs_inventory(files[:-1]), expected["romfs_inventory"])
                name, member = files[0]
                for changed in (replace(member, size=member.size + 1), replace(member, sha256="0" * 64)):
                    self.assertNotEqual(romfs_inventory([(name, changed), *files[1:]]), expected["romfs_inventory"])
                self.assertNotEqual(romfs_inventory([("renamed", member), *files[1:]]), expected["romfs_inventory"])

    def test_standalone_identity_version_resource_and_missing_tree_refusals(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path, expected, _ = self.fixture(folder, kind=1, version=10)
            original = path.read_bytes()
            for field, value, error in (("title_id", f"{TITLE_ID + 0x10000:016X}", "no base or matching"),
                                        ("ncm_version", 11, "version mismatch"),
                                        ("romfs_inventory", {"files": 999}, "RomFS inventory")):
                bad = {**expected, field: value}
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                    prepare(path, folder / field, bad, reserve_bytes=0)
                self.assertFalse((folder / field).exists())
            raw = bytearray(original)
            struct.pack_into("<I", raw, 0x80 + 0x30 + 0x10, 0xffffffff)
            refresh_toc_hash(raw);path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "Standalone update is incomplete"):
                prepare(path, folder / "missing-romfs", expected, reserve_bytes=0)
            self.assertFalse((folder / "missing-romfs").exists())

    def test_standalone_latest_selection_does_not_blend_older_or_unrelated_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            _, expected, payloads = self.fixture(folder, kind=1, version=10)
            files = [(n, data, 0) for n, data in payloads.items()]
            packages = [{"kind": 1, "flags": 1, "version": 9, "files": [("obsolete.bin", b"older", 0)]},
                        {"kind": 1, "flags": 1, "version": 10, "files": files},
                        {"kind": 1, "flags": 1, "version": 99, "title_id": TITLE_ID + 0x10000, "files": files}]
            def archive_of(packages, first_romfs=None):
                raw, _ = make_nup(packages)
                for i in range(len(packages)):
                    struct.pack_into("<I", raw, 0x80 + 0x30 + i * 0x40 + 0x10, i * 2 + 1)
                if first_romfs is not None:
                    struct.pack_into("<I", raw, 0x80 + 0x30 + 0x10, first_romfs)
                refresh_toc_hash(raw)
                path = folder / "selection.nup";path.write_bytes(raw)
                return Nup(path)
            with archive_of(packages) as archive:
                chosen, _, _ = verified_application(archive, expected)
                self.assertEqual([p.index for p in chosen], [1])
            with archive_of([*packages, packages[1]]) as archive:
                with self.assertRaisesRegex(ValueError, "ambiguous standalone"):
                    verified_application(archive, expected)
            with archive_of([packages[0], {**packages[1], "files": files[:1]}]) as archive:
                with self.assertRaisesRegex(ValueError, "ExeFS does not match"):
                    verified_application(archive, expected)
            with archive_of([packages[1], {**packages[1], "version": 11}]) as archive:
                with self.assertRaisesRegex(ValueError, "version mismatch"):
                    verified_application(archive, expected)
            with archive_of([packages[1], {**packages[2], "files": files[:1]}], first_romfs=3) as archive:
                # Complete ExeFS but a missing resource: point the selected
                # synthetic RomFS at a separate, partial tree in this fixture.
                with self.assertRaisesRegex(ValueError, "RomFS inventory"):
                    verified_application(archive, expected)
            dlc = {"kind": 2, "title_id": TITLE_ID + 0x1001, "files": [("dlc.bin", b"addon", 0)]}
            dlc_update = {"kind": 3, "title_id": TITLE_ID + 0x1001, "version": 65536,
                          "files": [("dlc.bin", b"addon v2", 0)]}
            with archive_of([packages[1], dlc, dlc_update]) as archive:
                # Merged DLC is skipped: only the standalone update is selected and
                # its resource inventory stays exactly the profiled one.
                chosen, _, selected = verified_application(archive, expected)
                self.assertEqual([p.index for p in chosen], [0])
                self.assertNotIn("dlc.bin", dict(selected))

    def test_base_update_with_merged_dlc_selects_application_only(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            payloads = {"raw.bin": b"raw fixture bytes", "z.bin": b"compressed fixture" * 1024}
            files = [(n, d, int(n == "z.bin")) for n, d in payloads.items()]
            packages = [{"kind": 0, "version": 0, "files": [("raw.bin", b"base copy", 0)]},
                        {"kind": 1, "version": 10, "title_id": TITLE_ID | 0x800, "files": files},
                        {"kind": 2, "title_id": TITLE_ID + 0x1001, "files": [("dlc.bin", b"addon", 0)]},
                        {"kind": 3, "title_id": TITLE_ID + 0x1001, "version": 65536,
                         "files": [("dlc.bin", b"addon v2", 0)]}]
            raw, _ = make_nup(packages)
            for i in range(len(packages)):
                struct.pack_into("<I", raw, 0x80 + 0x30 + i * 0x40 + 0x10, i * 2 + 1)
            refresh_toc_hash(raw)
            path = folder / "merged.nup"
            path.write_bytes(raw)
            original = path.read_bytes()
            expected = {"title_id": f"{TITLE_ID:016X}", "ncm_version": 10,
                        "exefs_sha256": {n: hashlib.sha256(d).hexdigest() for n, d in payloads.items()}}
            with Nup(path) as archive:
                expected["romfs_inventory"] = romfs_inventory(select_romfs(archive, archive.packages[:2]))
                chosen, _, selected = verified_application(archive, expected)
                self.assertEqual([p.index for p in chosen], [0, 1])
                self.assertEqual(sorted(dict(selected)), ["raw.bin", "z.bin"])
            result = prepare(path, folder / "romfs", expected, reserve_bytes=0)
            self.assertEqual(result["files"], 2)
            self.assertEqual(path.read_bytes(), original)
            manifest = json.loads((folder / "romfs/index.json").read_text())
            self.assertEqual(manifest["selected_packages"], [0, 1])

    def test_virtual_files_match_and_original_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path, expected, payloads = self.fixture(folder)
            original = path.read_bytes()
            out = folder / "romfs"
            result = prepare(path, out, expected, reserve_bytes=0)
            self.assertEqual(result["files"], 2)
            self.assertEqual(path.read_bytes(), original)
            manifest = json.loads((out / "index.json").read_text())
            cache = (out / "cache.bin").read_bytes()
            self.assertEqual(hashlib.sha256(cache).hexdigest(), manifest["cache_sha256"])
            for row in manifest["files"]:
                source, start = (original, row["offset"]) if row["storage"] == "nup" else (cache, row["offset"] - len(original))
                self.assertEqual(source[start:start + row["size"]], payloads[row["path"]])
            self.assertFalse((out / "romfs.bin").exists())
            built = folder / "build"
            count, _, _ = romfs_index.write(out, built)
            self.assertEqual(count, 2)
            self.assertEqual((built / "romfs-hashes.bin").stat().st_size, 64)
            self.assertIn("ROMFS_NUP_MODE 1", (built / "romfs_config.h").read_text())
            with self.assertRaisesRegex(ValueError, "exists"):
                prepare(path, out, expected, reserve_bytes=0)

    def test_wrong_exefs_prevents_cache_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path, expected, _ = self.fixture(folder)
            expected["exefs_sha256"]["raw.bin"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "match"):
                prepare(path, folder / "bad", expected, reserve_bytes=0)
            self.assertFalse((folder / "bad").exists())

    def test_cache_size_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path, expected, _ = self.fixture(folder)
            out = folder / "romfs"
            prepare(path, out, expected, reserve_bytes=0)
            (out / "cache.bin").write_bytes(b"truncated")
            with self.assertRaises(SystemExit):
                romfs_index.write(out, folder / "build")

    def test_path_traversal_rejected(self):
        with self.assertRaises(SystemExit):
            romfs_index.build_index({"romfs_size": 10, "files": [{"path": "a/../b", "offset": 0, "size": 1}]})

    def test_native_storage_boundary_and_overflow(self):
        compiler = Path("F:/llvm-mingw/bin/clang.exe")
        if not compiler.is_file():
            self.skipTest("test Clang not installed")
        source = '''#include "romfs_storage.h"
#include <assert.h>
int main(void) {
 int c; uint64_t o; uint32_t n;
 assert(romfs_storage_route(100,20,90,30,&c,&o,&n) && !c && o==90 && n==10);
 assert(romfs_storage_route(100,20,105,30,&c,&o,&n) && c && o==5 && n==15);
 assert(romfs_storage_route(100,20,120,1,&c,&o,&n) && n==0);
 assert(!romfs_storage_route(100,20,121,1,&c,&o,&n));
 assert(!romfs_storage_route(UINT64_MAX,1,0,1,&c,&o,&n));
 assert(romfs_storage_route(UINT64_C(50000000000),20,UINT64_C(50000000001),1,&c,&o,&n) && c && o==1);
 return 0;
}'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "check.c"
            path.write_text(source)
            exe = Path(temp) / "check.exe"
            subprocess.run([str(compiler), "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "runtime"), str(path), "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    unittest.main()
