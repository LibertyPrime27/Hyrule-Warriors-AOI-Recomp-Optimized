"""The game's own cover icon, taken from the user's NUP, as a Windows .ico for the compiled executable.

A NUP package's metadata carries the title's 256x256 JPEG icon (TLV kind 3). Nothing game-derived is committed: the
icon is read from the user's NUP at compile time and written under the ignored work/ folder.

usage: hwaoi_icon.py NUP OUT.ico [--update-nup NUP]
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hwrecomp.nup import Nup, KINDS  # noqa: E402

ICON_KIND = 3
SIZES = (256, 64, 48, 32, 16)
APPLICATION = {"base", "update"}


def icon_jpeg(nup_path):
    """The icon JPEG of the archive's application package (base or update), or None when it carries none."""
    with Nup(nup_path) as archive:
        for package in archive.packages:
            if KINDS.get(package.kind) not in APPLICATION or not package.meta_size:
                continue
            raw = archive._read(package.meta_offset, package.meta_size)
            offset = 0
            while offset + 8 <= len(raw):
                kind, _, size = struct.unpack_from("<HHI", raw, offset)
                payload = raw[offset + 8:offset + 8 + size]
                offset += 8 + size
                if kind == ICON_KIND and len(payload) == size and payload[:3] == b"\xff\xd8\xff":
                    return payload
    return None


# System.Drawing ships with every Windows .NET Framework, so decoding needs no extra download.
RESIZE = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$src = [System.Drawing.Image]::FromFile($env:HWAOI_ICON_SRC)
try {
  foreach ($size in $env:HWAOI_ICON_SIZES.Split(',')) {
    $n = [int]$size
    $bmp = New-Object System.Drawing.Bitmap $n, $n, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.DrawImage($src, 0, 0, $n, $n)
    $g.Dispose()
    $bmp.Save((Join-Path $env:HWAOI_ICON_DIR ("$n.png")), [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
  }
} finally { $src.Dispose() }
"""


def png_sizes(jpeg, sizes=SIZES):
    """{size: PNG bytes} resized from the JPEG with Windows' System.Drawing."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "icon.jpg"
        src.write_bytes(jpeg)
        env = dict(os.environ, HWAOI_ICON_SRC=str(src), HWAOI_ICON_DIR=tmp,
                   HWAOI_ICON_SIZES=",".join(str(s) for s in sizes))
        shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", RESIZE],
                       env=env, check=True, capture_output=True, timeout=120)
        return {s: (Path(tmp) / f"{s}.png").read_bytes() for s in sizes}


def ico(images):
    """An .ico holding PNG images (supported by Windows since Vista); {size: PNG bytes}, largest first."""
    order = sorted(images, reverse=True)
    header = struct.pack("<HHH", 0, 1, len(order))
    offset = 6 + 16 * len(order)
    entries, data = b"", b""
    for size in order:
        png = images[size]
        if png[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"icon image {size} is not a PNG")
        # Width/height 0 means 256.
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return header + entries + data


def write_icon(nup, out, update_nup=None):
    """Write OUT.ico from the NUP's icon (the update NUP's when the game NUP has none). Returns True when written."""
    jpeg = icon_jpeg(nup) or (icon_jpeg(update_nup) if update_nup else None)
    if not jpeg:
        return False
    data = ico(png_sizes(jpeg))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.is_file() or out.read_bytes() != data:   # unchanged icon: keep the timestamp, no relink
        out.write_bytes(data)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nup", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--update-nup", type=Path)
    args = ap.parse_args(argv)
    if not write_icon(args.nup, args.out, args.update_nup):
        print("hwaoi_icon: the NUP carries no icon", file=sys.stderr)
        return 1
    print(f"hwaoi_icon: wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
