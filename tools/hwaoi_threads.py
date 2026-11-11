"""Capture selected local Windows runtime threads; briefly pauses each thread.

The native helper copies registers/stack and resumes before any unwind or I/O.
Other threads keep running, so this is not an atomic whole-process snapshot.
No game memory is written. Output can contain game-derived addresses and stays
under ignored work/reports. This is a diagnostic, not a REA-produced capture.
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")
SYMBOLIZER = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/llvm-addr2line.exe")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def within(path, directory):
    path, directory = Path(path).resolve(), Path(directory).resolve()
    if path == directory or not path.is_relative_to(directory):
        raise ValueError(f"path must be below {directory}: {path}")
    return path


def process_image(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        text = ctypes.create_unicode_buffer(32768)
        count = wintypes.DWORD(len(text))
        if not kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(text.value).resolve(strict=True)
    finally:
        kernel.CloseHandle(handle)


def build_helper():
    source = ROOT / "tools/hwaoi_thread_snapshot.c"
    out = ROOT / "work/build/hwaoi-thread-snapshot"
    out.mkdir(parents=True, exist_ok=True)
    identity = digest(source)
    # Source-addressed helper names also permit a new build while an earlier
    # snapshot is still unwinding. Never replace a running helper executable.
    helper = out / f"snapshot-{identity[:16]}.exe"
    if not helper.is_file():
        subprocess.run([str(COMPILER), "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-municode",
                        str(source), "-ldbghelp", "-o", str(helper)], check=True, capture_output=True)
    return helper


def preferred_base(path):
    with path.open("rb") as file:
        file.seek(0x3c)
        offset = struct.unpack("<I", file.read(4))[0]
        file.seek(offset)
        if file.read(4) != b"PE\0\0":
            raise ValueError("not PE")
        file.seek(offset + 24)
        if file.read(2) != b"\x0b\x02":
            raise ValueError("not PE32+")
        file.seek(offset + 24 + 24)
        return struct.unpack("<Q", file.read(8))[0]


def symbolize(report, executable):
    base = preferred_base(executable)
    addresses = set()
    for thread in report["threads"]:
        for index, frame in enumerate(thread.get("frames", [])):
            pc = int(frame["pc"], 16)
            for module in report["modules"]:
                start = int(module["base"], 16)
                if start <= pc < start + module["size"]:
                    frame["module"] = module["path"]
                    frame["offset"] = hex(pc - start)
                    if Path(module["path"]).resolve() == executable:
                        # A caller's saved PC is the return address. Resolve the
                        # preceding byte for call-site ownership, never alter RIP.
                        frame["symbol_address"] = hex(base + pc - start - (1 if index else 0))
                        addresses.add(frame["symbol_address"])
                    break
    if not addresses or not SYMBOLIZER.is_file():
        return
    ordered = sorted(addresses)
    result = subprocess.run([str(SYMBOLIZER), "-e", str(executable), "-f", "-C"],
                            input="\n".join(ordered) + "\n", text=True, encoding="utf-8", errors="replace",
                            capture_output=True, timeout=30)
    if result.returncode:
        report["symbolizer_error"] = result.stderr
        return
    lines = result.stdout.splitlines()
    names = {a: lines[2*i:2*i+2] for i, a in enumerate(ordered)}
    for thread in report["threads"]:
        for frame in thread.get("frames", []):
            if frame.get("symbol_address") in names:
                frame["symbol"] = names[frame["symbol_address"]]


def capture(pid, output, expected_exe=None, expected_sha256=None):
    if os.name != "nt" or not 0 < pid <= 0xffffffff or pid == os.getpid():
        raise ValueError("positive, distinct Windows target PID required")
    output = within(output, ROOT / "work/reports")
    if output.exists():
        raise ValueError("refusing to overwrite an existing report")
    executable = within(process_image(pid), ROOT / "work/build")
    if expected_exe is not None and executable != Path(expected_exe).resolve(strict=True):
        raise ValueError("target executable path mismatch")
    identity = digest(executable)
    if expected_sha256 is not None and identity != expected_sha256.lower():
        raise ValueError("target executable SHA-256 mismatch")
    helper = build_helper()
    start = datetime.now(timezone.utc).isoformat()
    # Do not forcibly time out/kill a helper between SuspendThread/ResumeThread.
    # No helper code waits for a target lock; all unwind work happens afterwards.
    result = subprocess.run([str(helper), str(pid), str(executable)], capture_output=True,
                            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f"thread helper failed ({result.returncode}): {result.stderr}")
    report = json.loads(result.stdout)
    report.update({"captured_at_utc": start, "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                   "executable": str(executable), "sha256": identity, "helper_sha256": digest(helper),
                   "atomic_process_snapshot": False, "stack_limit_bytes": 256 * 1024,
                   "unwind_limit_frames": 64, "game_memory_written": False})
    if digest(executable) != identity:
        raise ValueError("target executable changed during capture")
    symbolize(report, executable)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    try:
        report = capture(args.pid, args.out, args.exe, args.sha256)
        print(json.dumps({"pid": report["pid"], "threads": len(report["threads"]), "report": str(args.out),
                          "errors": sum(any(t.get(k) for k in ("error", "context_error", "stack_error", "resume_error"))
                                        for t in report["threads"]),
                          "inventory_errors": sum(bool(report.get(k)) for k in
                                                  ("module_inventory_error", "thread_inventory_error"))}, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else error.stderr, file=sys.stderr)
        print(f"thread snapshot: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
