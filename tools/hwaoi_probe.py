"""Bounded Windows probes for the locally built HWAoI runtime.

Keeps stdout/stderr and exact executable identity. A Windows Job Object owns
the process tree and kills any surviving children on close. No boot guard is
changed by this tool. Each report gets its own fresh directory.
"""
from __future__ import annotations
import argparse
import ctypes as c
from ctypes import wintypes as w
import hashlib
import gzip
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def summarize_runtime_diagnostics(log):
    """Retain bounded owner evidence; these counts do not measure all process memory."""
    first = last = None
    samples = 0
    monotonic = True
    for match in re.finditer(r"^\[host\] runtime-memory: (frame=\d+[^\r\n]*)$", log, re.MULTILINE):
        row = {key: int(value) for key, value in re.findall(r"([a-z_]+)=(\d+)", match[1])}
        if "frame" not in row:
            continue
        if last is not None and row["frame"] <= last["frame"]:
            monotonic = False
        if first is None:
            first = row
        last = row
        samples += 1
    growth = {}
    if samples > 1 and monotonic:
        frames = last["frame"] - first["frame"]
        for key in ("copy_bytes", "zombie_bytes", "texture_pin_bytes", "program_control_bytes"):
            if key not in first or key not in last:
                continue
            if key == "program_control_bytes" and re.search(r"program_ledger_partial=[1-9]", log):
                continue
            growth[key] = (last[key] - first[key]) / frames
    rejected = re.findall(r"^\[host\] (vk: RGBA8 comparison rejected:[^\r\n]*)$", log, re.MULTILINE)
    fatals = re.findall(r"^FATAL: ([^\r\n]*)$", log, re.MULTILINE)
    return {"retained_owner_sample_count": samples,
            "retained_owner_first": first, "retained_owner_last": last,
            "retained_owner_growth_bytes_per_frame": growth,
            "last_rgba8_comparison_rejection": rejected[-1] if rejected else None,
            "last_fatal": fatals[-1] if fatals else None}


class BasicLimits(c.Structure):
    _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
                ("min_ws", c.c_size_t), ("max_ws", c.c_size_t), ("process_limit", w.DWORD),
                ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]


class IoCounters(c.Structure):
    _fields_ = [(name, c.c_uint64) for name in ("ro", "wo", "oo", "rb", "wb", "ob")]


class ExtendedLimits(c.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]


def close_owned_window(pid):
    """Request a graceful close of only this supervised child's windows."""
    user = c.WinDLL("user32", use_last_error=True)
    callback = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback, w.LPARAM]
    user.EnumWindows.restype = w.BOOL
    user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.PostMessageW.restype = w.BOOL
    count = 0

    @callback
    def visit(hwnd, unused):
        nonlocal count
        owner = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, c.byref(owner))
        if owner.value == pid and user.PostMessageW(hwnd, 0x10, 0, 0):
            count += 1
        return True

    user.EnumWindows(visit, 0)
    return count


def capture(executable, mode, report_dir, timeout, dump_shaders=False, frames=1,
            input_script=None, profile_sampling=False, threads_on_stall=False,
            shot_every=30, profile_main_ip=False, profile_frames=None, extended_play=False, input_feed=None,
            profile_dir=None):
    if os.name != "nt":
        raise ValueError("this supervised runner currently requires Windows")
    executable = executable.resolve(strict=True)
    report_dir = report_dir.resolve()
    if not executable.is_relative_to(ROOT / "work/build"):
        raise ValueError("target must be the local work/build output")
    if not report_dir.is_relative_to(ROOT / "work/reports") or report_dir == ROOT / "work/reports":
        raise ValueError("report directory must be a child of work/reports")
    if extended_play and mode != "--play-experimental":
        raise ValueError("extended deadline requires play-experimental")
    max_timeout = (1800 if extended_play else 180) if mode == "--play-experimental" else 60
    if report_dir.exists() or not 1 <= timeout <= max_timeout:
        raise ValueError(f"fresh report directory and timeout in 1..{max_timeout} seconds required")
    if not 1 <= frames <= 600:
        raise ValueError("frames must be in 1..600")
    if threads_on_stall and mode != "--play-experimental":
        raise ValueError("thread stall capture requires play-experimental")
    if not 0 <= shot_every <= 600 or (shot_every != 30 and mode != "--play-experimental"):
        raise ValueError("shot interval must be 0..600; changing it requires play-experimental")
    if threads_on_stall and not shot_every:
        raise ValueError("thread stall capture requires automatic screenshots")
    if profile_main_ip and mode != "--play-experimental":
        raise ValueError("main-thread IP sampling requires play-experimental")
    if profile_frames is not None:
        bounds = re.fullmatch(r"(0|[1-9][0-9]*)-([1-9][0-9]*)", profile_frames)
        if (not (profile_sampling or profile_main_ip) or not bounds or
                not 0 <= int(bounds[1]) < int(bounds[2]) <= 2**63 - 1):
            raise ValueError("profile frames require sampling and a valid LO-HI range")
    if input_script is not None:
        input_script = input_script.resolve(strict=True)
        if mode != "--play-experimental" or not input_script.is_relative_to(ROOT / "work"):
            raise ValueError("scripted diagnostic input requires play-experimental and a file under work/")
        script_bytes = input_script.read_bytes()
    if input_feed is not None:
        input_feed = input_feed.resolve(strict=True)
        if mode != "--play-experimental" or not input_feed.is_relative_to(ROOT / "work/reports") or not input_feed.is_file():
            raise ValueError("diagnostic input feed requires play-experimental and a file under work/reports")
    if profile_dir is not None:
        profile_dir = profile_dir.resolve(strict=True)
        if (mode != "--play-experimental" or not profile_dir.is_dir() or
                not profile_dir.is_relative_to(ROOT / "work/saves") or profile_dir == ROOT / "work/saves"):
            raise ValueError("diagnostic save profile must be an existing child of work/saves")
    with executable.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    kernel.TerminateJobObject.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    native = c.WinDLL("ntdll")
    native.NtResumeProcess.argtypes = [w.HANDLE]
    native.NtResumeProcess.restype = c.c_long
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise c.WinError(c.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000 | 0x400  # KILL_ON_JOB_CLOSE, DIE_ON_UNHANDLED_EXCEPTION
    proc = None
    try:
        if not kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)):
            raise c.WinError(c.get_last_error())
        report_dir.mkdir(parents=True)
        linker_map = None
        map_path = executable.parent / "totk.map"
        if map_path.is_file():
            map_bytes = map_path.read_bytes()
            retained_map = report_dir / "linker.map.gz"
            retained_map.write_bytes(gzip.compress(map_bytes, mtime=0))
            linker_map = {"path": str(retained_map), "sha256": hashlib.sha256(map_bytes).hexdigest(),
                          "executable_sha256": digest}
        if input_script is not None:
            input_script = report_dir / "input-script.txt"
            input_script.write_bytes(script_bytes)
        start = time.monotonic()
        timed_out = False
        env = dict(os.environ)
        for prefix in ("HWAOI_", "DKB_", "TOTK_"):
            env.pop(prefix + "INPUT_FEED", None)
        env["HWAOI_PROFILE_DIR"] = str(profile_dir or ROOT / "work/saves/probe")
        if input_feed is not None:
            env["HWAOI_INPUT_FEED"] = str(input_feed)
        if mode == "--probe-input":
            env.update({"HWAOI_INPUT": "none", "HWAOI_MOTION": "0", "TOTK_RUMBLE": "1"})
            # The fixture toggles legacy names to check both policies. Input is
            # disabled and all synthetic vibration amplitudes are zero.
            for setting in ("RUMBLE", "LANG", "LOCALE"):
                for prefix in ("HWAOI_", "DKB_"):
                    env.pop(prefix + setting, None)
        if mode == "--probe-boot":
            env["HWAOI_GPU"] = "null"  # initialization evidence only; stop at the first NVN request
        if mode in ("--probe-render", "--play-experimental"):
            env.update({"HWAOI_GPU": "vulkan", "HWAOI_FULLSCREEN": "0", "HWAOI_WINDOW_SIZE": "960x540",
                        "HWAOI_INPUT": "none", "HWAOI_RUMBLE": "0", "HWAOI_PIPE_SYNC": "1",
                        "HWAOI_PROBE_FRAME_PATH": str(report_dir / "frame.png")})
            # Failed inputs are local evidence for the next lowering fix. Keep
            # them under this ignored report even without a full shader dump.
            env["DKB_SHADER_FAILURE_DUMP"] = str(report_dir / "shader-failures")
            env["HWAOI_PROBE_PRESENTS"] = str(frames)
            env["HWAOI_SHOTS"] = f"1-{min(frames, 10)}:1"
            env["HWAOI_SHOTS_DIR"] = str(report_dir / "presents")
            # Intermediate images are small diagnostic snapshots; the target
            # frame.png remains full resolution. Do not inherit full-shot mode.
            for name in ("HWAOI_SHOTS_FULL", "DKB_SHOTS_FULL", "TOTK_SHOTS_FULL"):
                env.pop(name, None)
            if mode == "--play-experimental":
                # Endurance runs can exceed 100,000 presents. Keep observations
                # available for their full bounded lifetime, including 120 Hz.
                last_shot = timeout * 360 + shot_every
                env["HWAOI_SHOTS"] = f"{shot_every}-{last_shot}:{shot_every}" if shot_every else "0"
                env["HWAOI_PROGRESS"] = "1"
                if input_script is not None:
                    env["HWAOI_INPUT_SCRIPT"] = str(input_script)
            if profile_sampling or profile_main_ip:
                env["HWAOI_PROFILE"] = str(report_dir / "sampling.txt")
                # The inherited in-process stack unwinder crashes on this build.
                # Keep instruction-pointer sampling; use the external snapshot
                # tool for stacks without risking the game's address space.
                for setting in ("PROFILE_RENDER", "PROFILE_MAIN", "PROFILE_SLOW", "PROFILE_FRAMES"):
                    for prefix in ("HWAOI_", "DKB_", "TOTK_"):
                        env.pop(prefix + setting, None)
                env["HWAOI_PROFILE_CHAINS"] = "0"
                env["HWAOI_PROFILE_IP_ONLY"] = "1"
                if profile_main_ip:
                    env["HWAOI_PROFILE_MAIN"] = "1"
                if profile_frames is not None:
                    env["HWAOI_PROFILE_FRAMES"] = profile_frames
            translator = executable.parent / "xlate-managed-irbanks/totk_xlate.exe"
            ir_banks = translator.is_file() and translator.with_suffix(".id").is_file()
            if not ir_banks:
                translator = executable.parent / "xlate-managed/totk_xlate.exe"
            env["HWAOI_IR_BANKS"] = "1" if ir_banks else "0"
            if translator.is_file() and translator.with_suffix(".id").is_file():
                env["HWAOI_XLATE"] = str(translator)
                # The helper's source/mode identity must select the shader cache;
                # an unrelated caller override must not mislabel this probe.
                for name in ("HWAOI_XLATE_ID", "DKB_XLATE_ID", "TOTK_XLATE_ID"):
                    env.pop(name, None)
            if dump_shaders:
                env["DKB_SHADER_DUMP"] = str(report_dir / "nvn2-dump")
        with (report_dir / "stdout.log").open("wb") as stdout, (report_dir / "stderr.log").open("wb") as stderr:
            proc = subprocess.Popen([str(executable), mode], cwd=executable.parent, env=env,
                                    stdout=stdout, stderr=stderr,
                                    creationflags=subprocess.CREATE_NO_WINDOW | 0x4)  # CREATE_SUSPENDED
            if not kernel.AssignProcessToJobObject(job, w.HANDLE(int(proc._handle))):
                error = c.get_last_error()
                proc.kill()
                proc.wait(timeout=5)
                raise c.WinError(error)
            status = native.NtResumeProcess(w.HANDLE(int(proc._handle)))
            if status < 0:
                raise OSError(f"NtResumeProcess failed: {status & 0xffffffff:#x}")
            (report_dir / "running.json").write_text(json.dumps({"pid": proc.pid, "executable": str(executable),
                                                               "sha256": digest}) + "\n", encoding="utf-8")
            close_requested = 0
            thread_snapshots = []
            try:
                if not threads_on_stall:
                    code = proc.wait(timeout=timeout)
                else:
                    from hwaoi_threads import capture as capture_threads
                    deadline = time.monotonic() + timeout
                    last_image, changed, pair_due, captured = None, time.monotonic(), None, False
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(proc.args, timeout)
                        try:
                            code = proc.wait(timeout=min(1, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            pass
                        images = sorted((report_dir / "presents").glob("*.png"))
                        current = (images[-1].name, images[-1].stat().st_mtime_ns) if images else None
                        now = time.monotonic()
                        if current != last_image:
                            last_image, changed, captured = current, now, False
                        due = pair_due is not None and now >= pair_due
                        if len(thread_snapshots) < 4 and (due or (current and not captured and now - changed >= 8)):
                            path = report_dir / f"stall-threads-{len(thread_snapshots)+1:02}.json"
                            entry = {"path": str(path), "unchanged_capture_seconds": round(now-changed, 3)}
                            try:
                                capture_threads(proc.pid, path, executable, digest)
                            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                                entry["error"] = str(error)
                            thread_snapshots.append(entry)
                            pair_due = None if due else time.monotonic() + 2
                            captured = True
            except subprocess.TimeoutExpired:
                timed_out = True
                # Let the window thread flush diagnostics before the mandatory
                # Job Object cleanup. A hung shutdown remains bounded.
                close_requested = close_owned_window(proc.pid)
                try:
                    code = proc.wait(timeout=5 if close_requested else 0)
                except subprocess.TimeoutExpired:
                    if not kernel.TerminateJobObject(job, 124):
                        raise c.WinError(c.get_last_error())
                    code = proc.wait(timeout=5)
        options = {name: env[name] for name in (
            "HWAOI_OUTPUT_RES", "HWAOI_SCENE_RES", "HWAOI_FPS_LIMIT", "HWAOI_EXPERIMENTAL_RATE", "HWAOI_APP_POOL", "HWAOI_VK_PRESENT", "HWAOI_DISPLAY_POOL_TEST",
            "HWAOI_GSV_LOCAL_FRAME_POLICY", "HWAOI_NATIVE_INFLATE", "HWAOI_ZLIB_DLL",
            "HWAOI_GPU", "HWAOI_FIX_C0", "HWAOI_EXPERIMENTAL_GEOMETRY", "HWAOI_MEMORY", "HWAOI_SHADOW",
            "HWAOI_MOVIE_PACE",
            "HWAOI_POOL_DIAG", "HWAOI_POOL_BACKPRESSURE", "HWAOI_IR_BANKS", "HWAOI_XLATE",
            "HWAOI_TRACE", "HWAOI_NATIVE_INFLATE_TRACE", "HWAOI_RT_TRACE",
            "HWAOI_FS_DIAG", "HWAOI_PRIORITY_DIAG", "HWAOI_PROBE_ARRAYS_INDIRECT",
            "HWAOI_GPU_WRITE_DIAG", "HWAOI_GPU_READBACK_DIAG", "HWAOI_INDIRECT_BATCH", "HWAOI_UI_DRAW_DIAG", "HWAOI_RUNTIME_MEMORY_DIAG",
            "HWAOI_PERF_SLOW_GPU_MS",
            "HWAOI_INPUT_SCRIPT", "HWAOI_PROFILE", "HWAOI_PROFILE_CHAINS",
            "HWAOI_INPUT_FEED", "HWAOI_REPLAY_ASYNC", "HWAOI_VRAM_HEADROOM", "HWAOI_RATE_DIAG", "HWAOI_RATE_UNLOCK",
            "HWAOI_PROFILE_IP_ONLY", "HWAOI_PROFILE_MAIN", "HWAOI_PROFILE_FRAMES",
            "HWAOI_SHOTS", "HWAOI_SHOTS_FULL", "HWAOI_SHOTS_LATEST", "HWAOI_WINDOW_SIZE", "HWAOI_PIPE_SYNC", "HWAOI_PROFILE_DIR",
            "HWAOI_DUMP", "HWAOI_DUMP_DIR", "HWAOI_DUMP_LOG", "HWAOI_DUMP_UBO", "HWAOI_DUMP_TRIGGER",
            "HWAOI_DUMP_TEX", "HWAOI_DUMP_SKIP_RUNS", "HWAOI_DUMP_METADATA",
            "HWAOI_SCENE_SHARPNESS", "HWAOI_NATIVE_1080",
            "HWAOI_DUMP_SKIP_RUNS", "HWAOI_DUMP_TEX", "HWAOI_DUMP_BUF",
            "HWAOI_PROGRESS",
            "HWAOI_CORE_STALL_DIAG",
            "HWAOI_IMAGE_DIAG",
            "HWAOI_VK_VALIDATION", "DKB_VK_VALIDATION", "TOTK_VK_VALIDATION", "VK_LAYER_PATH",
            "DKB_SHADER_FAILURE_DUMP",
        ) if name in env}
        dependencies = {}
        for name in ("HWAOI_ZLIB_DLL", "HWAOI_XLATE"):
            if name not in options:
                continue
            dependency = Path(options[name])
            if dependency.is_absolute() and dependency.is_file():
                with dependency.open("rb") as stream:
                    dependencies[name] = {"path": str(dependency),
                                          "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
                if name == "HWAOI_XLATE":
                    # A managed apphost can stay byte-identical while its DLL changes.
                    # Retain the cache identity and managed payload, not only the launcher.
                    identity_path = dependency.with_suffix(".id")
                    if identity_path.is_file():
                        dependencies[name]["source_identity"] = identity_path.read_text(encoding="ascii").strip()
                    for payload in (dependency.with_suffix(".dll"),
                                    dependency.parent / "Ryujinx.Graphics.Shader.dll",
                                    dependency.parent / "build-report.json"):
                        if payload.is_file():
                            with payload.open("rb") as stream:
                                dependencies[name][payload.name] = hashlib.file_digest(stream, "sha256").hexdigest()
        result = {"executable": str(executable), "sha256": digest, "arguments": [mode],
                  "timeout_seconds": timeout, "extended_play": extended_play,
                  "runtime_options": options, "dependencies": dependencies,
                  "linker_map": linker_map,
                  "exit_code": code, "timed_out": timed_out, "seconds": round(time.monotonic()-start, 3),
                  "supervisor_close_requests": close_requested,
                  "thread_snapshots": thread_snapshots,
                  "stdout": str(report_dir / "stdout.log"), "stderr": str(report_dir / "stderr.log"),
                  "gameplay_verified": False}
        if input_script is not None:
            result["input_script_sha256"] = hashlib.sha256(script_bytes).hexdigest()
        if mode == "--play-experimental":
            log = (report_dir / "stderr.log").read_text(encoding="utf-8", errors="replace")
            result.update(summarize_runtime_diagnostics(log))
            progress = re.findall(r"HWAoI progress t=([0-9.]+) present=(\d+).*?shader=(\d+) lowering=(\d+) validation=(\d+) unimplemented=(\d+) skipped=(\d+)", log)
            result["last_progress"] = ({"guest_elapsed_seconds": float(progress[-1][0]),
                                        **dict(zip(("present", "shader", "lowering", "validation", "unimplemented", "skipped"),
                                                   map(int, progress[-1][1:])))} if progress else None)
            result["fatal_log_count"] = len(re.findall(r"^FATAL:", log, re.MULTILINE))
            result["lowering_failure_log_count"] = log.count("Ampere lowering failed")
            result["validation_error_log_count"] = log.count("vk validation error:")
            result["validation_enabled"] = "vk: validation layer and error reporting enabled" in log
            result["intermediate_captures"] = [str(p) for p in sorted((report_dir / "presents").glob("*.png"))]
        if mode == "--probe-render":
            log = (report_dir / "stderr.log").read_text(encoding="utf-8", errors="replace")
            presents = [int(n) for n in re.findall(r"HWAoI render probe (?:progress|observed) present (\d+)", log)]
            result.update({"requested_present": frames,
                           "validation_enabled": "vk: validation layer and error reporting enabled" in log,
                           "present_observed": bool(presents),
                           "last_present_observed": max(presents, default=0),
                           "target_present_observed": "HWAoI render probe observed present" in log,
                           "intermediate_captures": [str(p) for p in sorted((report_dir / "presents").glob("*.png"))],
                           "lowering_failure_log_count": log.count("Ampere lowering failed"),
                           "frame_capture": str(report_dir / "frame.png") if (report_dir / "frame.png").is_file() else None})
        (report_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result
    finally:
        if proc is not None and proc.poll() is None:
            kernel.TerminateJobObject(job, 125)
            proc.wait(timeout=5)
        kernel.CloseHandle(job)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["selftest", "probe-romfs", "probe-loader", "probe-cfg", "probe-kernel", "probe-services", "probe-input", "probe-storage", "probe-boot", "probe-render", "probe-present-color", "probe-vkgeometry", "play-experimental", "boot"])
    parser.add_argument("--exe", type=Path, default=ROOT / "work/build/hwaoi196608/Hyrule Warriors AOI Recomp Optimized.exe")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--dump-shaders", action="store_true", help="retain local game-derived shader inputs under this report")
    parser.add_argument("--frames", type=int, default=1, help="render probe stops at this present (1..600), still timeout-bounded")
    parser.add_argument("--input-script", type=Path, help="scripted input under work/ for a bounded experimental run")
    parser.add_argument("--profile-sampling", action="store_true", help="collect native instruction-pointer samples until graceful close")
    parser.add_argument("--threads-on-stall", action="store_true", help="capture a pair of native thread snapshots after 8 s without a new diagnostic image")
    parser.add_argument("--shot-every", type=int, default=30, help="experimental-play screenshot interval; 0 disables readbacks for timing")
    parser.add_argument("--profile-main-ip", action="store_true", help="sample only guest main-thread instruction pointers; requires an IP_ONLY-capable runtime")
    parser.add_argument("--profile-frames", help="sample only while presents LO..HI-1 are pending, e.g. 1400-2000")
    parser.add_argument("--extended-play", action="store_true", help="explicitly allow up to 1800 seconds for bounded movie/gameplay checks")
    parser.add_argument("--input-feed", type=Path, help="expiring live diagnostic commands from a file under work/reports")
    parser.add_argument("--profile-dir", type=Path, help="isolated existing diagnostic save profile under work/saves")
    args = parser.parse_args()
    try:
        result = capture(args.exe, "--" + args.mode, args.out, args.timeout, args.dump_shaders, args.frames,
                         args.input_script, args.profile_sampling, args.threads_on_stall,
                         args.shot_every, args.profile_main_ip, args.profile_frames, args.extended_play, args.input_feed,
                         args.profile_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["exit_code"] == 0 and not result["timed_out"] else 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"probe: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
