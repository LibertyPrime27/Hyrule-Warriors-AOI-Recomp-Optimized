"""Owned workspace synchronization and verified build-stage reuse."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from functools import lru_cache

STATE = "hwaoi-compiler.json"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_changed(path, data):
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{os.getpid()}.part")
    pending.write_bytes(data)
    pending.replace(path)


@lru_cache(maxsize=32)
def canonical_root(root):
    return root.resolve()


def inside(root, relative):
    boundary = canonical_root(root)
    path = (boundary / relative).resolve()
    require(path != boundary and path.is_relative_to(boundary), "Workspace path escaped its output folder")
    return path


@contextmanager
def workspace_lock(output):
    """An OS lock survives neither crashes nor cancellation; the file may remain."""
    import msvcrt
    path = inside(output, ".hwaoi-compiler.lock")
    with path.open("a+b") as lock:
        if not path.stat().st_size:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError("This output folder is already being compiled") from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def synchronize(bundle, output, files, destination, backup, previous=None):
    """Copy changed managed files only; preserve mtimes and back up replaced sources."""
    changed = 0
    for relative, expected in files.items():
        source = inside(bundle, relative)
        target = inside(output, destination(relative))
        if target.is_file() and digest(target) == expected:
            continue
        if target.exists():
            require(target.is_file(), f"Expected a file at {target}")
            saved = inside(output, backup / target.relative_to(output))
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_name(f".{target.name}.{os.getpid()}.part")
        shutil.copy2(source, pending)
        require(digest(pending) == expected, f"Copied file failed its hash check: {relative}")
        os.utime(pending, None)  # repaired/updated inputs must be newer than old objects
        pending.replace(target)
        changed += 1
    for relative in (previous or {}).keys() - files.keys():
        target = inside(output, destination(relative))
        if target.exists():
            require(target.is_file(), f"Expected a managed file at {target}")
            saved = inside(output, backup / target.relative_to(output))
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            target.unlink()  # file only, resolved and checked inside this workspace
            changed += 1
    return changed


def prepare_tools(bundle, output, manifest, backup):
    files = {p: sha for p, sha in manifest["files"].items()
             if p.startswith(("llvm/", "deps/")) or p == "bin/ninja.exe"}
    require(files and "llvm/bin/clang.exe" in files and "bin/ninja.exe" in files, "Missing compiler inputs")
    def destination(relative):
        return Path("work/toolchain") / ("ninja.exe" if relative == "bin/ninja.exe" else relative)
    stamp = inside(output, "work/toolchain/identity.json")
    try:
        previous = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    changed = synchronize(bundle, output, files, destination, backup, previous.get("files"))
    if changed or previous.get("identity") != key(files):
        write_changed(stamp, json.dumps({"identity": key(files), "installed": time.time_ns(), "files": files}).encode("ascii"))
    return stamp


def generation_key(output, manifest, unlock):
    files = {}
    for pattern in ("recomp/*.py", "recomp/patches-hwaoi196608-*.txt", "config/games/hwaoi*.json"):
        for path in output.glob(pattern):
            files[path.relative_to(output).as_posix()] = digest(path)
    for name in ("tools/hwaoi.py", "tools/hostmem.py", "tools/check_content.py"):
        files[name] = digest(output / name)
    return key({"sources": files, "unlock": unlock,
                "python": {p: sha for p, sha in manifest["files"].items() if p.startswith("python/")}})


def generated_files(folder):
    return {p.relative_to(folder).as_posix(): digest(p) for p in sorted(folder.rglob("*"))
            if p.is_file() and not p.name.startswith(".") and p.suffix in (".c", ".h", ".json", ".txt")}


def resource_cache(folder, identity, exefs, nup, previous, prepared=False):
    """Changed cache files get a full hash check; unchanged verified caches stay fast."""
    try:
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
        cache = folder / "cache.bin"
        require(index.get("status") == "pass" and index.get("storage") == "nup-cache-v1", "Incomplete cache")
        require(index.get("exefs_sha256") == exefs and index.get("nup_header_sha256") == identity["header"] and
                index.get("nup_toc_sha256") == identity["toc"] and index.get("nup_size") == identity["size"] and
                Path(index["nup_path"]).resolve() == nup.resolve(), "Cache source mismatch")
        update = identity.get("update")
        recorded = index.get("update_nup")
        require((update is None and recorded is None) or (
                update is not None and recorded is not None and recorded.get("size") == update["size"] and
                recorded.get("header_sha256") == update["header"] and recorded.get("toc_sha256") == update["toc"]),
                "Cache update source mismatch")
        stat = cache.stat()
        require(stat.st_size == index["cache_size"], "Cache size mismatch")
        stamp = {"size": stat.st_size, "mtime": stat.st_mtime_ns, "ctime": stat.st_ctime_ns,
                 "index": digest(folder / "index.json")}
        if stamp != previous and not prepared:
            require(digest(cache) == index["cache_sha256"], "Cache hash mismatch")
        return stamp
    except (OSError, ValueError, KeyError, TypeError):
        return None


def quarantine(output, path):
    """Preserve an incomplete derived stage inside work/, without deleting user data."""
    if not path.exists():
        return
    work = (output / "work").resolve()
    require(work.is_relative_to(output.resolve()) and path.resolve().is_relative_to(work) and
            not path.is_symlink() and not path.is_junction(),
            "Refusing to move a stage outside this workspace")
    target = path.with_name(path.name + f"-incomplete-{time.time_ns()}")
    require(target.resolve().is_relative_to(work) and not target.exists(), "Invalid stage backup path")
    path.rename(target)
