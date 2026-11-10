"""The game icon taken from a NUP's package metadata (tools/hwaoi_icon.py) and its .rc build edge."""
from pathlib import Path
import os
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests" / "prototype"))
import hwaoi_icon  # noqa: E402
from test_formats import make_nup, refresh_toc_hash  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n"
CLANG = Path(os.environ.get("HWAOI_TEST_CLANG", "F:/llvm-mingw/bin/clang.exe"))
NINJA = Path(os.environ.get("HWAOI_TEST_NINJA", "F:/HWAoI-Compiler/dev/bin/ninja.exe"))


def with_metadata(raw, tlvs, package=0):
    """Append a metadata TLV block after the blobs and point package `package` at it."""
    raw = bytearray(raw)
    meta = b"".join(struct.pack("<HHI", kind, 0, len(payload)) + payload for kind, payload in tlvs)
    offset = len(raw)
    raw += meta
    toc = struct.unpack_from("<Q", raw, 8)[0]
    struct.pack_into("<QI", raw, toc + 0x30 + package * 0x40 + 32, offset, len(meta))
    struct.pack_into("<Q", raw, 0x40, len(raw))   # the header's declared file size
    refresh_toc_hash(raw)
    return bytes(raw)


def jpeg(size=8):
    """A small real JPEG made with the same System.Drawing the icon conversion uses."""
    script = ("Add-Type -AssemblyName System.Drawing; $b = New-Object System.Drawing.Bitmap %d, %d; "
              "$g = [System.Drawing.Graphics]::FromImage($b); $g.Clear([System.Drawing.Color]::FromArgb(200, 30, 40)); "
              "$g.Dispose(); $b.Save($env:OUT, [System.Drawing.Imaging.ImageFormat]::Jpeg)" % (size, size))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "x.jpg"
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                       env=dict(os.environ, OUT=str(out)), capture_output=True, timeout=120)
        return out.read_bytes()


class IcoTests(unittest.TestCase):
    def test_png_entries_largest_first_with_256_as_zero(self):
        images = {16: PNG + b"a", 256: PNG + b"bbbb", 32: PNG + b"cc"}
        data = hwaoi_icon.ico(images)
        self.assertEqual(struct.unpack_from("<HHH", data), (0, 1, 3))
        entries = [struct.unpack_from("<BBBBHHII", data, 6 + 16 * i) for i in range(3)]
        self.assertEqual([e[0] for e in entries], [0, 32, 16])
        for e, size in zip(entries, (256, 32, 16)):
            self.assertEqual(data[e[7]:e[7] + e[6]], images[size])

    def test_rejects_non_png(self):
        with self.assertRaises(ValueError):
            hwaoi_icon.ico({16: b"GIF89a"})


class MetadataTests(unittest.TestCase):
    def nup(self, tlvs, dlc=False):
        """A standalone update (kind 1, flag 1); with dlc, a base plus a DLC package that carries the TLVs."""
        packages = ([{"kind": 0, "files": [("main", b"game", 0)]},
                     {"kind": 2, "title_id": 0x04003F40232DF001, "files": [("data", b"dlc", 0)]}] if dlc else
                    [{"kind": 1, "flags": 1, "files": [("main", b"game", 0)]}])
        raw, _ = make_nup(packages)
        tmp = tempfile.NamedTemporaryFile(suffix=".nup", delete=False)
        tmp.write(with_metadata(raw, tlvs, package=1 if dlc else 0))
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_finds_the_jpeg_among_other_records(self):
        icon = b"\xff\xd8\xff\xe0fixture-jpeg"
        path = self.nup([(1, b"META" + bytes(12)), (3, icon), (5, b"Title")])
        self.assertEqual(hwaoi_icon.icon_jpeg(path), icon)

    def test_no_icon_record_or_not_a_jpeg(self):
        self.assertIsNone(hwaoi_icon.icon_jpeg(self.nup([(5, b"Title")])))
        self.assertIsNone(hwaoi_icon.icon_jpeg(self.nup([(3, b"not a jpeg")])))

    def test_dlc_package_icon_is_ignored(self):
        self.assertIsNone(hwaoi_icon.icon_jpeg(self.nup([(3, b"\xff\xd8\xff\xe0dlc")], dlc=True)))

    def test_missing_icon_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "icon.ico"
            self.assertFalse(hwaoi_icon.write_icon(self.nup([(5, b"Title")]), out))
            self.assertFalse(out.exists())

    @unittest.skipUnless(sys.platform == "win32", "System.Drawing conversion is Windows-only")
    def test_end_to_end_ico_from_a_real_jpeg(self):
        path = self.nup([(3, jpeg())])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "icon.ico"
            self.assertTrue(hwaoi_icon.write_icon(path, out))
            data = out.read_bytes()
            count = struct.unpack_from("<HHH", data)[2]
            self.assertEqual(count, len(hwaoi_icon.SIZES))
            for i in range(count):
                _, _, _, _, _, _, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
                self.assertEqual(data[offset:offset + 8], PNG)
            stamp = out.stat().st_mtime_ns
            self.assertTrue(hwaoi_icon.write_icon(path, out))   # identical icon: file left untouched
            self.assertEqual(out.stat().st_mtime_ns, stamp)


class BuildEdgeTests(unittest.TestCase):
    def test_icon_rc_edge_and_quoted_spaced_output(self):
        import build_game
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            icon = root / "icon dir" / "game.ico"
            icon.parent.mkdir()
            icon.write_bytes(b"ico")
            out = root / "out"
            gen = root / "gen"
            gen.mkdir()
            a = gen / "main_0000.c"
            a.write_text("int x;\n")
            # DEPS set (as for a real --toolchain build) so the flags need no system VMA and nothing is fetched.
            deps = root / "deps"
            (deps / "include").mkdir(parents=True)
            with patch.multiple(build_game, ROOT=root, MAIN_ROOT=root, ICON=icon, WIN=True, DEPS=deps,
                                VMA_LOCAL=root / "no-fetch"), \
                    patch.object(build_game, "runtime_sources", side_effect=lambda *args: []), \
                    patch.object(build_game, "vma_include", side_effect=lambda: deps / "include"):
                text, objs, exe = build_game.ninja_file(gen, out, {"main": [a]}, 2)
            self.assertEqual(exe.name, "Hyrule Warriors AOI Recomp Optimized.exe")
            self.assertIn("build out/obj/game_icon.o: rc out/game_icon.rc | icon$ dir/game.ico", text)
            self.assertIn("@$out.rsp", text)
            self.assertNotIn('"$out', text)   # Ninja already quotes $out; extra quotes split the name (1.0.0 bug)
            self.assertIn("build out/Hyrule$ Warriors$ AOI$ Recomp$ Optimized.exe: link", text)
            self.assertIn(out / "obj" / "game_icon.o", objs["runtime"])
            self.assertEqual((out / "game_icon.rc").read_text(encoding="utf-8"), f'1 ICON "{icon.resolve().as_posix()}"\n')

    @unittest.skipUnless(sys.platform == "win32" and CLANG.is_file() and NINJA.is_file(), "Windows Clang and Ninja")
    def test_real_link_rule_links_an_executable_whose_name_has_spaces(self):
        """Run build_game's own link rule through Ninja and Clang, as the compiler does."""
        import build_game
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="hwaoi link ") as tmp:
            root = Path(tmp)
            gen = root / "gen"
            gen.mkdir()
            src = gen / "main_0000.c"
            src.write_text("int main(void){return 7;}\n")
            out = root / "out dir"
            deps = root / "deps"
            (deps / "include").mkdir(parents=True)
            with patch.multiple(build_game, ROOT=root, MAIN_ROOT=root, WIN=True, DEPS=deps, ICON=None,
                                CLANG=CLANG, VMA_LOCAL=root / "no-fetch"), \
                    patch.object(build_game, "runtime_sources", side_effect=lambda *args: []), \
                    patch.object(build_game, "vma_include", side_effect=lambda: deps / "include"):
                text, _, exe = build_game.ninja_file(gen, out, {"main": [src]}, 1)
            link = [line for line in text.splitlines() if line.startswith("rule link")]
            self.assertEqual(link, ["rule link"])
            start = text.splitlines().index("rule link")
            rule_lines = text.splitlines()[start:start + 5]
            obj = out / "obj" / "main" / "main_0000.o"
            obj.parent.mkdir(parents=True)
            subprocess.run([str(CLANG), "-c", str(src), "-o", str(obj)], check=True)
            ninja_text = "\n".join([f"cc = {build_game.cmd_arg(CLANG.as_posix())}", "ldflags =", "libs =", *rule_lines, "",
                                    f"build {build_game.ninja_path(exe)}: link {build_game.ninja_path(obj)}", ""])
            (root / "build.ninja").write_text(ninja_text)
            result = subprocess.run([str(NINJA)], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(exe.name, "Hyrule Warriors AOI Recomp Optimized.exe")
            self.assertEqual(subprocess.run([str(exe)]).returncode, 7)


if __name__ == "__main__":
    unittest.main()
