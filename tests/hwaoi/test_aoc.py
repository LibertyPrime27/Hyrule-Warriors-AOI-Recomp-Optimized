"""DLC (add-on content) installation from NUPs and folders, plus the native aoc.c mount/read path."""
import hashlib
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests/prototype"))
from test_formats import make_nup, refresh_toc_hash, TITLE_ID
import hwaoi_aoc
from hwrecomp.nup import FormatError

AOC_BASE = (TITLE_ID & ~0xFFF) + 0x1000


def nup_with(folder, packages):
    raw, _ = make_nup(packages)
    for i in range(len(packages)):   # each synthetic package tree also serves as its RomFS
        struct.pack_into("<I", raw, 0x80 + 0x30 + i * 0x40 + 0x10, i * 2 + 1)
    refresh_toc_hash(raw)
    path = folder / "game.nup"
    path.write_bytes(raw)
    return path


def manifest(folder):
    lines = (folder / "manifest.tsv").read_text(encoding="utf-8").splitlines()
    return lines[0], [line.split("\t") for line in lines[1:]]


class AocTests(unittest.TestCase):
    def game(self):
        return {"kind": 1, "flags": 1, "version": 10, "files": [("main", b"game", 0)]}

    def test_index_mapping(self):
        self.assertEqual(hwaoi_aoc.aoc_index(TITLE_ID, AOC_BASE + 1), 1)
        self.assertEqual(hwaoi_aoc.aoc_index(TITLE_ID, AOC_BASE + 2047), 2047)
        for other in (AOC_BASE, AOC_BASE + 2048, TITLE_ID, TITLE_ID | 0x800, AOC_BASE + 0x10000 + 1):
            self.assertIsNone(hwaoi_aoc.aoc_index(TITLE_ID, other))

    def test_installs_addon_with_newest_data_patch_and_keeps_nup(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            big = b"z" * 70000
            path = nup_with(folder, [
                self.game(),
                {"kind": 2, "title_id": AOC_BASE + 1, "files": [("b.bin", big, 1), ("x.bin", b"old", 0)]},
                {"kind": 3, "title_id": AOC_BASE + 1, "version": 65536, "files": [("x.bin", b"v1", 0)]},
                {"kind": 3, "title_id": AOC_BASE + 1, "version": 131072, "files": [("x.bin", b"v2!", 0)]},
                {"kind": 2, "title_id": AOC_BASE + 3, "files": [("c.bin", b"three", 0)]},
                {"kind": 2, "title_id": AOC_BASE + 0x20000, "files": [("other.bin", b"x", 0)]},   # another title
                {"kind": 3, "title_id": AOC_BASE + 5, "files": [("orphan.bin", b"x", 0)]},        # patch, no add-on
            ])
            original = path.read_bytes()
            logs = []
            installed = hwaoi_aoc.install_from_nup(path, folder / "aoc", TITLE_ID, log=logs.append)
            self.assertEqual(installed, {1: 2, 3: 1})
            self.assertEqual(path.read_bytes(), original)
            head, rows = manifest(folder / "aoc/1")
            self.assertEqual(head, f"HWAOI-AOC\t1\t{AOC_BASE + 1:016X}\t131072")
            self.assertEqual([r[2] for r in rows], ["b.bin", "x.bin"])
            self.assertEqual((folder / "aoc/1/romfs/x.bin").read_bytes(), b"v2!")
            self.assertEqual((folder / "aoc/1/romfs/b.bin").read_bytes(), big)
            for sha, size, name in rows:
                data = (folder / "aoc/1/romfs" / name).read_bytes()
                self.assertEqual((sha, int(size)), (hashlib.sha256(data).hexdigest(), len(data)))
            self.assertTrue(any("not an add-on" in line for line in logs))
            self.assertTrue(any("without its add-on" in line for line in logs))
            self.assertFalse(list((folder / "aoc").glob(".*.part")))
            # A second run over the same NUP reuses the installed folders.
            again = hwaoi_aoc.install_from_nup(path, folder / "aoc", TITLE_ID, log=logs.append)
            self.assertEqual(again, {1: None, 3: None})

    def test_no_dlc_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            logs = []
            path = nup_with(folder, [self.game()])
            self.assertEqual(hwaoi_aoc.install_from_nup(path, folder / "aoc", TITLE_ID, log=logs.append), {})
            self.assertFalse((folder / "aoc").exists())
            self.assertTrue(any("no add-on content" in line for line in logs))

    def test_corrupt_member_leaves_no_installed_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path = nup_with(folder, [self.game(), {"kind": 2, "title_id": AOC_BASE + 1, "files": [("d.bin", b"payload!", 0)]}])
            raw = bytearray(path.read_bytes())
            at = raw.rfind(b"payload!")
            raw[at] ^= 1
            path.write_bytes(raw)
            with self.assertRaisesRegex(FormatError, "SHA-256"):
                hwaoi_aoc.install_from_nup(path, folder / "aoc", TITLE_ID, log=lambda _: None)
            self.assertFalse((folder / "aoc/1").exists())

    def test_ambiguous_patches_and_duplicate_addons_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            addon = {"kind": 2, "title_id": AOC_BASE + 1, "files": [("d.bin", b"x", 0)]}
            patch = {"kind": 3, "title_id": AOC_BASE + 1, "version": 7, "files": [("d.bin", b"y", 0)]}
            for packages, error in (([self.game(), addon, patch, patch], "ambiguous"),
                                    ([self.game(), addon, addon], "more than one")):
                with self.subTest(error=error), self.assertRaisesRegex(FormatError, error):
                    hwaoi_aoc.install_from_nup(nup_with(folder, packages), folder / "aoc", TITLE_ID, log=lambda _: None)

    def test_install_from_extracted_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            src = folder / "dlc"
            (src / "sub").mkdir(parents=True)
            (src / "sub/b.bin").write_bytes(b"bee")
            (src / "a.bin").write_bytes(b"a")
            self.assertEqual(hwaoi_aoc.install_from_dir(src, folder / "aoc", 2, TITLE_ID, version=65536), 2)
            head, rows = manifest(folder / "aoc/2")
            self.assertEqual(head, f"HWAOI-AOC\t1\t{AOC_BASE + 2:016X}\t65536")
            self.assertEqual([r[2] for r in rows], ["a.bin", "sub/b.bin"])
            with self.assertRaisesRegex(FormatError, "index"):
                hwaoi_aoc.install_from_dir(src, folder / "aoc", 0, TITLE_ID)
            shutil.rmtree(src)
            src.mkdir()
            with self.assertRaisesRegex(FormatError, "empty"):
                hwaoi_aoc.install_from_dir(src, folder / "aoc", 4, TITLE_ID)

    def test_native_aoc_mount_list_read_and_integrity(self):
        compiler = Path("F:/llvm-mingw/bin/clang.exe")
        if not compiler.is_file():
            self.skipTest("test Clang not installed")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            src = folder / "dlc"
            (src / "data/deep").mkdir(parents=True)
            (src / "data/one.bin").write_bytes(b"0123456789")
            (src / "data/deep/two.bin").write_bytes(b"two")
            (src / "top.bin").write_bytes(b"top")
            hwaoi_aoc.install_from_dir(src, folder / "aoc", 5, TITLE_ID)
            hwaoi_aoc.install_from_dir(src, folder / "aoc", 2, TITLE_ID)
            exe = folder / "aoc_fixture.exe"
            subprocess.run([str(compiler), "-std=c11", "-Wall", "-Wextra", "-Werror", "-DAOC_NO_OVERRIDES",
                            "-I", str(ROOT / "runtime"), str(ROOT / "tests/hwaoi/aoc_fixture.c"),
                            str(ROOT / "runtime/aoc.c"), str(ROOT / "runtime/sha256.c"), "-o", str(exe)], check=True)
            env = {"HWAOI_AOC_DIR": str(folder / "aoc"), "SYSTEMROOT": "C:\\Windows"}
            result = subprocess.run([str(exe)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # A changed byte is caught on first open, before the game sees it.
            (folder / "aoc/2/romfs/top.bin").write_bytes(b"TOP")
            result = subprocess.run([str(exe), "tampered"], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not match its manifest SHA-256", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
