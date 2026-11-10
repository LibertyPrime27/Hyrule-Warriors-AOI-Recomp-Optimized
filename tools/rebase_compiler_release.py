"""Build a new compiler release from an earlier release's toolchain and this checkout's source.

The game-independent parts of a release (bundled Python, LLVM, runtime dependencies, Ninja, zlib, the shader
translator and the licenses) rarely change; the fork's source does. This takes those parts from an extracted earlier
release ZIP, adds the committed source exactly as tools/package_hwaoi_compiler.py selects it, rebuilds the launcher
with the bundled Clang, writes START-HERE.txt and a fresh bundle.json, scans the content and zips it. It runs in the
release workflow (.github/workflows/release-compiler.yml), so publishing needs no local upload.

usage: rebase_compiler_release.py BASE_RELEASE_DIR OUT_DIR ZIP VERSION
"""
from __future__ import annotations
import argparse
import ast
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import package_hwaoi_compiler as pkg  # noqa: E402

LAUNCHER = "Hyrule Warriors AOI Recomp Optimized Compiler.exe"
CARRIED = ("python", "llvm", "deps", "translator", "bin", "licenses")


def start_here_text():
    """START-HERE.txt exactly as the packager writes it."""
    tree = ast.parse((ROOT / "tools/package_hwaoi_compiler.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "write_text" and
                isinstance(node.func.value, ast.BinOp) and getattr(node.func.value.right, "value", "") == "START-HERE.txt"):
            return node.args[0].value
    raise ValueError("START-HERE text not found in package_hwaoi_compiler.py")


def build(base: Path, out: Path, zip_path: Path, version: str):
    base, out = base.resolve(strict=True), out.resolve()
    if out.exists() or zip_path.exists():
        raise ValueError("Output folder or ZIP already exists")
    manifest = json.loads((base / "bundle.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "hwaoi-compiler-v1" or manifest.get("game_content_included") is not False:
        raise ValueError("The base is not a compiler release")
    for relative, expected in manifest["files"].items():   # the carried toolchain must be the published one
        if relative.split("/")[0] in CARRIED and pkg.digest(base / relative) != expected:
            raise ValueError(f"Base release file changed: {relative}")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT).strip():
        raise ValueError("Commit the source first, so the recorded revision identifies the shipped source")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    out.mkdir(parents=True)
    for name in CARRIED:
        shutil.copytree(base / name, out / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pdb"))
    for relative in pkg.source_files():
        target = out / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    subprocess.run([str(out / "llvm/bin/clang.exe"), "-O2", "-mwindows", "-municode", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "compiler/launcher.c"), "-o", str(out / LAUNCHER)], check=True)
    (out / "START-HERE.txt").write_text(start_here_text(), encoding="utf-8")
    pkg.scan_release(out)
    files = {p.relative_to(out).as_posix(): pkg.digest(p) for p in sorted(out.rglob("*")) if p.is_file()}
    (out / "bundle.json").write_text(json.dumps({"format": "hwaoi-compiler-v1", "version": version,
                                                 "source_revision": revision, "files": files,
                                                 "game_content_included": False}, indent=2) + "\n", encoding="utf-8")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(out.rglob("*")):
            if path.is_file():
                archive.write(path, Path(out.name) / path.relative_to(out))
    zip_path.with_suffix(".zip.sha256").write_text(pkg.digest(zip_path) + "  " + zip_path.name + "\n", encoding="ascii")
    print(json.dumps({"output": str(out), "zip": str(zip_path), "files": len(files), "version": version,
                      "source_revision": revision}))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", type=Path, help="extracted earlier release folder (holds bundle.json)")
    ap.add_argument("out", type=Path)
    ap.add_argument("zip", type=Path)
    ap.add_argument("version")
    args = ap.parse_args(argv)
    try:
        build(args.base, args.out, args.zip, args.version)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"rebase_compiler_release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
