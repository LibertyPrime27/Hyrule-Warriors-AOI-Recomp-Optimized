"""Install HWAoI add-on content (DLC) folders for the runtime's MountAddOnContent (runtime/aoc.c).

Layout of one installed add-on, <aoc root>/<index>/:
    manifest.tsv    HWAOI-AOC<TAB>1<TAB><add-on title id hex><TAB><NCM version>, then one
                    <sha256><TAB><size><TAB><path> row per file, sorted by UTF-8 path bytes
    romfs/<path>    the add-on's RomFS files
    source.json     where the files came from (lets a rebuild skip an unchanged add-on)

Two sources:
    from-nup  NUP AOC_ROOT                     addon (kind 2) + newest addon_update (kind 3) packages of HWAoI
    from-dir  ROMFS_DIR AOC_ROOT --index N     an add-on RomFS you extracted yourself (e.g. from a DLC NSP)

Add-on title ids are application id + 0x1000 + index (index 1..2047). The original NUP is only read.
Every file is hashed while it is written; the runtime hashes it again on first open.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hwrecomp.nup import Nup, ABSENT, require

FORMAT = "HWAOI-AOC\t1"
INDEX_MAX = 2047
COPY_CHUNK = 8 << 20


def aoc_base(application_id):
    return (application_id & ~0xFFF) + 0x1000


def aoc_index(application_id, title_id):
    """AddOnContentIndex of title_id, or None when it is not an add-on of this application."""
    index = title_id - aoc_base(application_id)
    return index if 1 <= index <= INDEX_MAX else None


def select_addons(archive, application_id):
    """{index: (title_id, version, [packages oldest-first])}: each add-on plus its newest data patch.

    Add-ons of other titles and addon_updates without their add-on are reported and skipped.
    """
    found, skipped = {}, []
    for p in archive.packages:
        if p.kind not in (2, 3):
            continue
        index = aoc_index(application_id, p.title_id)
        if index is None:
            skipped.append(f"{p.title_id:016X} (not an add-on of {application_id:016X})")
            continue
        if p.romfs_dir == ABSENT:
            skipped.append(f"{p.title_id:016X} kind {p.kind} (no RomFS)")
            continue
        found.setdefault(index, {2: [], 3: []})[p.kind].append(p)
    result = {}
    for index, kinds in sorted(found.items()):
        require(len(kinds[2]) <= 1, f"add-on {index}: more than one addon package")
        if not kinds[2]:
            skipped.append(f"add-on {index} (data patch without its add-on)")
            continue
        chosen = list(kinds[2])
        if kinds[3]:
            newest = max(p.version for p in kinds[3])
            latest = [p for p in kinds[3] if p.version == newest]
            require(len(latest) == 1, f"add-on {index}: ambiguous data patches at version {newest}")
            chosen += latest
        result[index] = (chosen[0].title_id, chosen[-1].version, chosen)
    return result, skipped


def addon_files(archive, packages):
    """Merged (path, Member) rows: a data patch shadows its add-on by path."""
    selected = {}
    for package in packages:
        selected.update(dict(archive.walk(package.romfs_dir)))
    for name in selected:
        parts = name.split("/")
        require(all(p and p not in (".", "..") for p in parts), "invalid add-on RomFS path")
        require(not any("/".join(parts[:i]) in selected for i in range(1, len(parts))), "add-on file/directory conflict")
    return sorted(selected.items(), key=lambda item: item[0].encode("utf-8"))


def safe_path(root, name):
    target = (root / name).resolve()
    require(target.is_relative_to(root.resolve()), f"add-on path escapes its folder: {name}")
    return target


def write_manifest(folder, title_id, version, rows):
    rows = sorted(rows, key=lambda r: r[2].encode("utf-8"))
    require(len({r[2] for r in rows}) == len(rows) and rows, "add-on manifest needs unique paths")
    lines = [f"{FORMAT}\t{title_id:016X}\t{version}"] + [f"{sha}\t{size}\t{path}" for sha, size, path in rows]
    (folder / "manifest.tsv").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def copy_member(archive, member, target):
    """Writes member to target, verifying its SHA-256 (raw members are streamed)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if member.codec == 1:
        target.write_bytes(archive.read_member(member))   # bounded and hash-checked by the reader
        return
    digest = hashlib.sha256()
    with target.open("xb") as out:
        done = 0
        while done < member.size:
            chunk = archive._read(member.offset + done, min(COPY_CHUNK, member.size - done))
            digest.update(chunk)
            out.write(chunk)
            done += len(chunk)
    require(digest.hexdigest() == member.sha256, f"add-on member SHA-256 mismatch: {member.name}")


def install_from_nup(nup_path, aoc_root, application_id, log=print):
    """Installs every HWAoI add-on in the NUP under aoc_root. Returns {index: file count}."""
    aoc_root = Path(aoc_root)
    installed = {}
    with Nup(nup_path) as archive:
        addons, skipped = select_addons(archive, application_id)
        for item in skipped:
            log(f"  DLC: skipped {item}")
        if not addons:
            log("  DLC: no add-on content in this NUP (place extracted DLC with tools/hwaoi_aoc.py from-dir)")
            return installed
        for index, (title_id, version, packages) in addons.items():
            source = {"format": 1, "nup_header_sha256": archive.header_sha256, "nup_toc_sha256": archive.toc_sha256,
                      "packages": [p.index for p in packages], "title_id": f"{title_id:016X}", "version": version}
            folder = aoc_root / str(index)
            previous = folder / "source.json"
            if previous.is_file() and (folder / "manifest.tsv").is_file():
                try:
                    if json.loads(previous.read_text(encoding="utf-8")) == source:
                        log(f"  DLC {index}: reusing installed add-on {title_id:016X} v{version}")
                        installed[index] = None
                        continue
                except ValueError:
                    pass
            files = addon_files(archive, packages)
            size = sum(m.size for _, m in files)
            aoc_root.mkdir(parents=True, exist_ok=True)
            require(shutil.disk_usage(aoc_root).free >= size + (1 << 30),
                    f"DLC {index} needs {size} bytes plus 1 GiB free")
            staging = aoc_root / f".{index}.part"
            if staging.exists():
                shutil.rmtree(staging)
            (staging / "romfs").mkdir(parents=True)
            rows = []
            for name, member in files:
                copy_member(archive, member, safe_path(staging / "romfs", name))
                rows.append((member.sha256, member.size, name))
            write_manifest(staging, title_id, version, rows)
            (staging / "source.json").write_text(json.dumps(source), encoding="utf-8")
            if folder.exists():
                shutil.rmtree(folder)
            os.replace(staging, folder)
            installed[index] = len(rows)
            log(f"  DLC {index}: installed add-on {title_id:016X} v{version}, {len(rows)} files, {size / 2**30:.2f} GiB")
    return installed


def install_from_dir(romfs_dir, aoc_root, index, application_id, version=0):
    """Copies an extracted add-on RomFS into <aoc_root>/<index>. Returns the file count."""
    require(1 <= index <= INDEX_MAX, f"add-on index must be 1..{INDEX_MAX}")
    romfs_dir, aoc_root = Path(romfs_dir).resolve(strict=True), Path(aoc_root)
    require(romfs_dir.is_dir(), "add-on RomFS must be a folder")
    staging = aoc_root / f".{index}.part"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "romfs").mkdir(parents=True)
    rows = []
    for path in sorted(romfs_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(romfs_dir).as_posix()
        require(all(p and p not in (".", "..") for p in name.split("/")), f"invalid path {name}")
        target = safe_path(staging / "romfs", name)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as src, target.open("xb") as out:
            while chunk := src.read(COPY_CHUNK):
                digest.update(chunk)
                out.write(chunk)
                size += len(chunk)
        rows.append((digest.hexdigest(), size, name))
    require(bool(rows), "add-on RomFS folder is empty")
    title_id = aoc_base(application_id) + index
    write_manifest(staging, title_id, version, rows)
    (staging / "source.json").write_text(json.dumps({"format": 1, "folder": str(romfs_dir), "title_id": f"{title_id:016X}",
                                                     "version": version}), encoding="utf-8")
    folder = aoc_root / str(index)
    if folder.exists():
        shutil.rmtree(folder)
    os.replace(staging, folder)
    return len(rows)


def main(argv=None):
    profile = json.loads((Path(__file__).resolve().parents[1] / "config/games/hwaoi-update-196608.json").read_text("utf-8"))
    application_id = int(profile["title_id"], 16)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("from-nup", help="install the add-ons stored in a NUP")
    a.add_argument("nup")
    a.add_argument("aoc_root", help="e.g. <output>/work/game/aoc")
    b = sub.add_parser("from-dir", help="install an extracted add-on RomFS folder")
    b.add_argument("romfs_dir")
    b.add_argument("aoc_root")
    b.add_argument("--index", type=int, required=True, help="AddOnContentIndex (title id - application id - 0x1000)")
    b.add_argument("--version", type=int, default=0, help="the add-on's NCM version (informational)")
    args = parser.parse_args(argv)
    if args.command == "from-nup":
        installed = install_from_nup(args.nup, args.aoc_root, application_id)
        print(json.dumps({"installed": sorted(installed)}))
    else:
        count = install_from_dir(args.romfs_dir, args.aoc_root, args.index, application_id, args.version)
        print(json.dumps({"installed": [args.index], "files": count}))


if __name__ == "__main__":
    main()
