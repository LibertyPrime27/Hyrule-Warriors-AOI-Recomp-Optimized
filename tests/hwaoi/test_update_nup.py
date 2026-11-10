"""A base-only game NUP plus a separate update NUP (and an optional DLC NUP) through the compiler's preparation."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests/prototype"))
from test_formats import make_nup, refresh_toc_hash, TITLE_ID
from compiler import backend
from hwaoi_romfs import prepare, romfs_inventory, verified_game
from hwrecomp.nup import Nup, FormatError

AOC_BASE = (TITLE_ID & ~0xFFF) + 0x1000
# Synthetic package trees serve as both ExeFS and RomFS. Names stay sorted (the NUP reader requires it).
BASE = {"base.bin": b"only in the base", "main": b"base main", "main.npdm": b"npdm", "rtld": b"rtld", "sdk": b"base sdk",
        "subsdk0": b"subsdk0"}
UPDATE = {"main": b"updated main", "patch.bin": b"only in the update" * 50, "sdk": b"updated sdk"}


def write_nup(path, packages):
    raw, _ = make_nup(packages)
    for i in range(len(packages)):
        struct.pack_into("<I", raw, 0x80 + 0x30 + i * 0x40 + 0x10, i * 2 + 1)
    refresh_toc_hash(raw)
    path.write_bytes(raw)
    return path


def files(payloads, compressed=()):
    return [(n, d, int(n in compressed)) for n, d in sorted(payloads.items())]


class UpdateNupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.game = write_nup(self.root / "game.nup", [{"kind": 0, "version": 0, "files": files(BASE)}])
        self.update = write_nup(self.root / "update.nup", [{"kind": 1, "flags": 1, "title_id": TITLE_ID | 0x800,
                                                            "version": 10, "files": files(UPDATE, {"sdk"})}])
        merged = {**BASE, **UPDATE}
        self.merged = merged
        self.expected = {"title_id": f"{TITLE_ID:016X}", "ncm_version": 10,
                         "exefs_sha256": {n: hashlib.sha256(d).hexdigest() for n, d in merged.items()}}
        with Nup(self.game) as g, Nup(self.update) as u:
            rows = {**dict(g.walk(g.packages[0].romfs_dir)), **dict(u.walk(u.packages[0].romfs_dir))}
        self.expected["romfs_inventory"] = romfs_inventory(sorted(rows.items(), key=lambda r: r[0].encode()))

    def tearDown(self):
        self.temp.cleanup()

    def test_base_plus_separate_update_verifies_as_one_game(self):
        with Nup(self.game) as g, Nup(self.update) as u:
            chosen, exefs, rows = verified_game(g, self.expected, u)
            self.assertEqual([(a is g, p.kind) for a, p in chosen], [(True, 0), (False, 1)])
            self.assertIs(exefs["main"][0], u)
            self.assertIs(exefs["rtld"][0], g)
            self.assertEqual([name for name, _, _ in rows], sorted(self.merged))
        with Nup(self.game) as g, self.assertRaisesRegex(FormatError, "version mismatch"):
            verified_game(g, self.expected)   # the base alone is not the supported build

    def test_preparation_copies_update_files_into_the_cache_and_keeps_sources(self):
        before = (self.game.read_bytes(), self.update.read_bytes())
        out = self.root / "romfs"
        with contextlib.redirect_stdout(io.StringIO()):
            result = prepare(self.game, out, self.expected, reserve_bytes=0, update_path=self.update)
        self.assertEqual(result["files"], len(self.merged))
        self.assertEqual((self.game.read_bytes(), self.update.read_bytes()), before)
        index = json.loads((out / "index.json").read_text())
        self.assertEqual(index["update_nup"]["size"], len(before[1]))
        game_bytes, cache = before[0], (out / "cache.bin").read_bytes()
        for row in index["files"]:
            if row["storage"] == "nup":
                data = game_bytes[row["offset"]:row["offset"] + row["size"]]
            else:
                start = row["offset"] - len(game_bytes)
                data = cache[start:start + row["size"]]
            self.assertEqual(data, self.merged[row["path"]], row["path"])
            # Everything from the update NUP is in the cache: the game runs without the update file.
            self.assertEqual(row["storage"] == "cache", row["path"] in UPDATE, row["path"])

    def test_refusals_name_the_wrong_input(self):
        other = write_nup(self.root / "other.nup", [{"kind": 1, "flags": 1, "title_id": TITLE_ID + 0x10000,
                                                     "version": 10, "files": files(UPDATE)}])
        older = write_nup(self.root / "older.nup", [{"kind": 1, "flags": 1, "title_id": TITLE_ID | 0x800,
                                                     "version": 9, "files": files(UPDATE)}])
        for game, update, error in ((self.game, other, "contains no HWAoI update"),
                                    (self.game, older, "version mismatch"),
                                    (self.update, self.update, "must contain the HWAoI base game")):
            with self.subTest(error=error), Nup(game) as g, Nup(update) as u, self.assertRaisesRegex(FormatError, error):
                verified_game(g, self.expected, u)

    def test_compiler_identity_exefs_and_dlc_nup(self):
        profile = {**self.expected, "modules": {}, "npdm_sha256": ""}
        dlc = write_nup(self.root / "dlc.nup", [{"kind": 2, "title_id": AOC_BASE + 1, "files": [("dlc.bin", b"addon", 0)]}])
        with unittest.mock.patch.object(backend, "nup_profile", return_value=profile), \
             contextlib.redirect_stdout(io.StringIO()):
            identity = backend.inspect_nup(self.game, self.update)
            self.assertEqual(set(identity), {"header", "toc", "size", "update"})
            self.assertEqual(identity["update"]["size"], self.update.stat().st_size)
            self.assertNotEqual(identity, backend.inspect_nup(self.game, self.update) | {"update": None})
            backend.extract_game_exefs(self.game, self.root / "exefs", self.update)
            self.assertEqual((self.root / "exefs/main").read_bytes(), UPDATE["main"])
            self.assertEqual((self.root / "exefs/rtld").read_bytes(), BASE["rtld"])
            backend.inspect_dlc_nup(dlc, (self.game, self.update))
            with self.assertRaisesRegex(ValueError, "no HWAoI DLC"):
                backend.inspect_dlc_nup(self.update, (self.game,))
            with self.assertRaisesRegex(ValueError, "different file"):
                backend.inspect_dlc_nup(self.game, (self.game,))
            installed = backend.install_game_dlc([self.game, self.update, dlc], self.root / "out")
            self.assertEqual(installed, {1: 1})
            self.assertEqual((self.root / "out/work/game/aoc/1/romfs/dlc.bin").read_bytes(), b"addon")


if __name__ == "__main__":
    import unittest.mock   # noqa: F401  (patch in test_compiler_identity_exefs_and_dlc_nup)
    unittest.main()
