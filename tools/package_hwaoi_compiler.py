"""Assemble the portable HWAoI compiler from existing tools; never include a built game.

This command only packages locally. Publishing a release is a separate, explicit action.
The release includes the full fork source and licenses, Python/Tk, the x64 compiler,
runtime dependencies and the game-independent shader translator. No system settings change.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_content

ROOT = Path(__file__).resolve().parents[1]
LICENSES = {
    "ninja-COPYING.txt": ("https://raw.githubusercontent.com/ninja-build/ninja/v1.13.2/COPYING",
                          "eb7e9ab9690124c5c9f42bdc81383d886a3dede26345b6ed15bbad7caf81f7ea"),
    "libvpx-LICENSE.txt": ("https://raw.githubusercontent.com/webmproject/libvpx/v1.17.0/LICENSE",
                          "8267348d5af1262c11d1a08de2f5afc77457755f1ac658627dd9acf71011d615"),
    "libvpx-PATENTS.txt": ("https://raw.githubusercontent.com/webmproject/libvpx/v1.17.0/PATENTS",
                          "cc3273e0694ea5896145e0677699b53471b03ea43021ddc50e7923fbb9f5023c"),
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy(src, dst):
    if not src.is_file():
        raise ValueError(f"Missing package input: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def tree(src, dst, skip=None):
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_file() and "__pycache__" not in rel.parts and path.suffix not in (".pyc", ".pdb") and not (skip and skip(rel)):
            copy(path, dst / rel)


def source_files():
    # Only files eligible for source control; never descend into work/, build/ or refs/.
    raw = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    for name in sorted(set(raw.decode("utf-8").split("\0")) - {""}):
        path = Path(name)
        if path.parts[0] in ("compiler", "config", "recomp", "runtime", "tools", "tests") or name in (
                "LICENSE", "LICENSE-MIT", "THIRD_PARTY_NOTICES.md", "README.md", "VERSION", "AGENTS.md"):
            problems = check_content.problems_for(ROOT / path)
            if problems:
                raise ValueError(f"Source content check failed: {name}: {problems}")
            yield path


def scan_release(folder):
    problems = []
    for path in folder.rglob("*"):
        if path.is_file():
            problems.extend(f"{path.relative_to(folder)}: {p}" for p in check_content.problems_for(path, allow_large=True))
    if problems:
        raise ValueError("Release content check failed:\n" + "\n".join(problems))


def package(args):
    out = args.out.resolve()
    if out.exists():
        raise ValueError("Package output already exists; choose a fresh folder")
    if (out == ROOT or out.is_relative_to(ROOT)) and not out.is_relative_to(ROOT / "work"):
        raise ValueError("Within the checkout, package output must be under ignored work/")
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT)
    if dirty.strip():
        raise ValueError("Commit the source changes before packaging, so the recorded revision identifies the shipped source")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    out.mkdir(parents=True)
    for relative in source_files():
        copy(ROOT / relative, out / "source" / relative)
    # Standard-library Python with only the packages the verified NUP/generator needs.
    for path in args.python.iterdir():
        if path.is_file() and (path.suffix.lower() in (".exe", ".dll") or path.name == "LICENSE.txt"):
            copy(path, out / "python" / path.name)
    tree(args.python / "DLLs", out / "python/DLLs")
    tree(args.python / "tcl", out / "python/tcl")
    tree(args.python / "Lib", out / "python/Lib", lambda rel: rel.parts[0] in ("site-packages", "test", "idlelib", "ensurepip"))
    site = args.python / "Lib/site-packages"
    for path in sorted(site.iterdir()):
        if path.name in ("numpy", "numpy.libs", "lz4", "zstandard") or path.name.startswith(("numpy-", "lz4-", "zstandard-")):
            tree(path, out / "python/Lib/site-packages" / path.name)
    for name in ("bin", "include", "lib", "x86_64-w64-mingw32"):
        tree(args.llvm / name, out / "llvm" / name)
    copy(args.llvm / "LICENSE.TXT", out / "llvm/LICENSE.TXT")
    tree(args.deps, out / "deps", lambda rel: rel.name == "libiconv-2.dll")
    tree(args.translator, out / "translator", lambda rel: rel.name == "build-report.json")
    copy(args.ninja, out / "bin/ninja.exe")
    copy(args.zlib, out / "bin/zlib1.dll")
    licenses = out / "licenses"
    for path in (args.deps_licenses).rglob("*"):
        if path.is_file() and ("licenses" in path.parts or path.name in ("LICENSE.txt", "PATENTS")):
            copy(path, licenses / "dependencies" / path.relative_to(args.deps_licenses))
    copy(args.zlib.parent.parent / "share/licenses/zlib/LICENSE", licenses / "zlib-LICENSE.txt")
    copy(args.dotnet / "LICENSE.txt", licenses / "dotnet-LICENSE.txt")
    copy(args.dotnet / "ThirdPartyNotices.txt", licenses / "dotnet-ThirdPartyNotices.txt")
    for path in (args.dotnet / "nuget").rglob("*"):
        if path.is_file() and path.name.lower() in ("license", "license.txt", "license.md", "thirdpartynotices.txt"):
            copy(path, licenses / "nuget" / path.relative_to(args.dotnet / "nuget"))
    for name, (url, expected) in LICENSES.items():
        cached = args.license_cache / name
        if not cached.is_file():
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(urllib.request.urlopen(url, timeout=30).read())
        if digest(cached) != expected:
            raise ValueError(f"License download hash mismatch: {name}")
        copy(cached, licenses / name)
    subprocess.run([str(args.llvm / "bin/clang.exe"), "-O2", "-mwindows", "-municode", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "compiler/launcher.c"), "-o", str(out / "Hyrule Warriors AOI Recomp Optimized Compiler.exe")], check=True)
    (out / "START-HERE.txt").write_text(
        "Hyrule Warriors AOI Recomp Optimized - Compiler (Windows x64)\n\n"
        "1. Extract this complete ZIP to a writable folder. No Python/.NET/compiler installation is needed.\n"
        "2. Open Hyrule Warriors AOI Recomp Optimized Compiler.exe. Select your own matching NUP and an output folder on a drive with 20+ GiB free.\n"
        "3. Click Compile / update. After completion, click Open compiled game.\n"
        "   To reopen a build later, set Output folder to its exact path and click Open compiled game.\n\n"
        "Updates/resume: choose the existing output folder (new builds create \"Hyrule Warriors AOI Recomp Optimized\"). Verified game data/generated C and native objects\n"
        "are reused. Changed managed source files are backed up under work/backups; saves and settings are preserved.\n"
        "Build jobs 0 selects concurrency from current RAM/commit headroom. Large C units run one at a time.\n\n"
        "Only title 04003F40232DE000 / NCM 196608 with the exact module hashes in source/config/games is supported.\n"
        "Complete standalone-update NUPs without a base record are accepted after module/resource inventory checks.\n"
        "Partial updates need the matching base game. DLC/addon packages merged into the NUP are installed\n"
        "under work/game/aoc and offered to the game as installed add-on content.\n"
        "Optional fields: Update file (a separate update NUP for a base-only game file) and DLC file (a DLC NUP).\n"
        "Encrypted XCI/NSP files and other builds are not supported. No game, keys or firmware is included.\n"
        "Keep the source NUP and compiled output at their original paths. Output and logs remain local.\n"
        "The compiled game needs an AVX2 CPU, Vulkan graphics and sufficient RAM/VRAM (32 GiB RAM recommended).\n"
        "1080p/60 FPS is the baseline. The 90/120 FPS settings SPEED UP the game (1.5x/2x), because gameplay advances a\n"
        "fixed step per frame. For smoother motion at normal speed, keep 60 FPS and use frame generation (for example\n"
        "Lossless Scaling 2x). Higher resolutions are unstable.\n"
        "The game's icon is taken from your own NUP while compiling; no artwork ships with this ZIP.\n"
        "Progress and tested limits: source/README.md. Licenses/source: source/ and licenses/.\n"
        "The locally compiled output contains game-derived files; do not upload or redistribute it.\n",
        encoding="utf-8")
    print("Checking release content...", flush=True)
    scan_release(out)
    files = {p.relative_to(out).as_posix(): digest(p) for p in sorted(out.rglob("*")) if p.is_file()}
    manifest = {"format": "hwaoi-compiler-v1", "version": args.version, "source_revision": revision,
                "files": files, "game_content_included": False}
    (out / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(out), "files": len(files), "bytes": sum(p.stat().st_size for p in out.rglob("*") if p.is_file())}))
    if args.zip:
        if args.zip.exists():
            raise ValueError("ZIP already exists")
        args.zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.zip, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(out.rglob("*")):
                if path.is_file():
                    archive.write(path, Path(out.name) / path.relative_to(out))
        args.zip.with_suffix(".zip.sha256").write_text(digest(args.zip) + "  " + args.zip.name + "\n", encoding="ascii")
        print(f"ZIP: {args.zip} ({args.zip.stat().st_size} bytes)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--zip", type=Path)
    parser.add_argument("--version", default="0.1.0-alpha.1")
    parser.add_argument("--python", type=Path, default=Path("F:/Python"))
    parser.add_argument("--llvm", type=Path, default=Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw"))
    parser.add_argument("--deps", type=Path, default=Path("F:/TotKDependencies/runtime/deps"))
    parser.add_argument("--deps-licenses", type=Path, default=Path("F:/TotKDependencies/unpacked"))
    parser.add_argument("--ninja", type=Path, default=Path("F:/Ninja/ninja.exe"))
    parser.add_argument("--zlib", type=Path, default=Path("F:/GCC/ucrt64/bin/zlib1.dll"))
    parser.add_argument("--dotnet", type=Path, default=Path(os.environ.get("HWAOI_TOOLS", "F:") + "/DotNet"))
    parser.add_argument("--translator", type=Path, default=ROOT / "work/build/hwaoi196608/xlate-managed-irbanks")
    parser.add_argument("--license-cache", type=Path, default=Path("F:/HWAoICompiler/licenses"))
    args = parser.parse_args()
    try:
        package(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
