"""Local NUP-to-native build workflow used by the portable compiler window."""
from __future__ import annotations
import argparse
import contextlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import traceback
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "tools"))
from hwrecomp.nup import Nup, extract_exefs
from hwaoi_romfs import verified_application
import hwaoi_aoc
import hwaoi
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
from compiler import cache

STATE = "hwaoi-compiler.json"
RESOLUTIONS = ("1280x720", "1920x1080", "2560x1440", "3840x2160")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def save(path, value):
    pending = path.with_suffix(path.suffix + ".part")
    pending.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pending.replace(path)


def bundle_check(bundle, *, defer_materialized=False):
    """Validate the bundle; build-only inputs may be hash-checked during synchronization."""
    bundle = Path(bundle).resolve(strict=True)
    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    require(manifest.get("format") == "hwaoi-compiler-v1", "Unsupported compiler bundle")
    for relative, expected in manifest["files"].items():
        path = (bundle / relative).resolve()
        require(path.is_relative_to(bundle) and path.is_file(), f"Missing bundle file: {relative}")
        critical_source = relative.startswith(("source/compiler/", "source/tools/hwrecomp/", "source/config/games/")) or relative in (
            "source/tools/hwaoi.py", "source/tools/hwaoi_romfs.py", "source/tools/hwaoi_aoc.py")
        deferred = defer_materialized and not critical_source and (relative.startswith(("source/", "llvm/", "deps/", "translator/")) or
                                           relative in ("bin/ninja.exe", "bin/zlib1.dll"))
        if not deferred:
            require(hwaoi.sha256(path) == expected, f"Bundle file changed: {relative}. Extract the ZIP again.")
    for relative in ("python/python.exe", "llvm/bin/clang.exe", "bin/ninja.exe",
                     "bin/zlib1.dll", "translator/totk_xlate.exe", "translator/totk_xlate.id",
                     "deps/include/vma/vk_mem_alloc.h", "source/tools/hwaoi.py", "source/tools/hwaoi_romfs.py"):
        require(relative in manifest["files"], f"Incomplete bundle: {relative}")
    return manifest


def nup_profile():
    profile = json.loads((SOURCE / "config/games/hwaoi-update-196608.json").read_text(encoding="utf-8"))
    return {**profile, "exefs_sha256": {**profile["modules"], "main.npdm": profile["npdm_sha256"]}}


def inspect_nup(path):
    """Reject the wrong title/build or incomplete resources before extraction."""
    with Nup(path) as archive:
        for package in archive.packages[:16]:
            kind = {0: "base", 1: "update", 2: "addon", 3: "addon_update", 4: "system"}[package.kind]
            print(f"  NUP package {package.index}: {kind}, title={package.title_id:016X}, "
                  f"NCM={package.version}, standalone={bool(package.flags & 1)}", flush=True)
        packages, _, _ = verified_application(archive, nup_profile())
        addons = sum(p.kind in (2, 3) for p in archive.packages)
        if addons:
            print(f"  Found {addons} DLC/addon package(s); they are installed for the game after the base resources.",
                  flush=True)
        if packages[0].kind == 1:
            print("  No base record: using the standalone update with the complete verified game inventory.", flush=True)
        return {"header": archive.header_sha256, "toc": archive.toc_sha256, "size": archive.file_size}


def extract_game_exefs(nup, output):
    with Nup(nup) as archive:
        # Keep extraction on exactly the same selection as preflight and RomFS.
        packages, _, _ = verified_application(archive, nup_profile())
        return extract_exefs(archive, output, packages[0].index if len(packages) == 1 else None)


def install_game_dlc(nup, output):
    """Installs the NUP's HWAoI add-ons as work/game/aoc/<index> for runtime/aoc.c; unchanged ones are reused."""
    return hwaoi_aoc.install_from_nup(nup, Path(output) / "work/game/aoc", int(nup_profile()["title_id"], 16),
                                      log=lambda line: print(line, flush=True))


def fresh_output(output, bundle, nup, allow_lock=False):
    output, bundle, nup = Path(output).resolve(), Path(bundle).resolve(), Path(nup).resolve()
    require(not output.is_relative_to(bundle) and not bundle.is_relative_to(output),
            "Choose an output folder outside the extracted compiler folder")
    require(not nup.is_relative_to(output), "The output folder must not contain the source NUP")
    require(not output.exists() or (output.is_dir() and all(allow_lock and p.name == ".hwaoi-compiler.lock"
            and p.is_file() and p.stat().st_size <= 1 for p in output.iterdir())),
            "Output folder is not empty. Choose a new empty folder; existing files are never overwritten.")
    parent = output
    while not parent.exists():
        parent = parent.parent
    require(shutil.disk_usage(parent).free >= 20 << 30, "At least 20 GiB free is required on the output drive")
    return output


def clean_environment(bundle):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HWAOI_", "DKB_", "TOTK_", "PYTHON"))}
    env["PYTHONUTF8"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    # No dependency on tools installed on the build author's PC or on the user's PATH.
    env["PATH"] = os.pathsep.join(str(p) for p in (
        bundle / "llvm/bin", bundle / "bin", Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"))
    return env


def run_tool(bundle, output, *arguments):
    command = [str(bundle / "python/python.exe"), "-E", "-s", str(output / "tools/hwaoi.py"), *map(str, arguments)]
    print("+ " + subprocess.list2cmdline(command), flush=True)
    process = subprocess.Popen(command, cwd=output, env=clean_environment(bundle), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    for line in process.stdout:
        print(line, end="", flush=True)
    process.stdout.close()
    code = process.wait()
    require(code == 0, f"Build step failed (exit {code}). See compiler.log for details.")


class Tee:
    def __init__(self, stream, log):
        self.stream, self.log = stream, log
    def write(self, value):
        if self.stream:
            self.stream.write(value)
            self.stream.flush()
        self.log.write(value)
        self.log.flush()
    def flush(self):
        self.log.flush()


def build(bundle, nup, output, jobs=0, resolution="1920x1080", unlock=False):
    started = time.perf_counter()
    require(os.name == "nt" and platform.machine().lower() in ("amd64", "x86_64"), "Windows x64 is required")
    require(0 <= jobs <= 8 and resolution in RESOLUTIONS, "Invalid build settings")
    bundle = Path(bundle).resolve(strict=True)
    nup = Path(nup).resolve(strict=True)
    print("[1/6] Checking compiler files and game identity...", flush=True)
    manifest = bundle_check(bundle, defer_materialized=True)
    checked = time.perf_counter()
    print(f"  Bundle preflight: {checked - started:.2f}s; managed inputs verified before use.", flush=True)
    identity = inspect_nup(nup)
    before = nup.stat()
    output = Path(output).resolve()
    marker = output / STATE
    existing = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else None
    if existing is None:
        output = fresh_output(output, bundle, nup, allow_lock=True)
    else:
        require(isinstance(existing, dict) and existing.get("format") == "hwaoi-local-build-v1" and existing.get("nup_identity") == identity and
                Path(existing.get("nup", "")).resolve() == nup, "This output belongs to a different source NUP")
        require(not output.is_relative_to(bundle) and not bundle.is_relative_to(output) and not nup.is_relative_to(output),
                "Choose an output folder outside the compiler and source NUP")
        require(shutil.disk_usage(output).free >= 2 << 30, "Updating needs at least 2 GiB free")
    output.mkdir(parents=True, exist_ok=True)
    with cache.workspace_lock(output):
        # Recheck after acquiring the lock, before replacing managed source files.
        if marker.is_file():
            current = json.loads(marker.read_text(encoding="utf-8"))
            require(current.get("nup_identity") == identity and current.get("format") == "hwaoi-local-build-v1" and
                    Path(current.get("nup", "")).resolve() == nup,
                    "Output identity changed while waiting for its lock")
            existing = current
        else:
            require(all(p.name == ".hwaoi-compiler.lock" for p in output.iterdir()), "Unmarked output folder is not empty")
        game = output / "work/build/hwaoi196608"
        if (game / "hwaoi.exe").is_file():
            try:
                with (game / "hwaoi.exe").open("r+b"):
                    pass
            except OSError as error:
                raise ValueError("Close the compiled game before updating this output folder") from error
        state = dict(existing or {})
        state.update(format="hwaoi-local-build-v1", status="incomplete", source_revision=manifest["source_revision"],
                     nup=str(nup), nup_identity=identity, resolution=resolution, rate_unlock=bool(unlock))
        save(marker, state)
        backup = Path("work/backups") / f"compiler-{time.time_ns()}"
        sources = {p: sha for p, sha in manifest["files"].items() if p.startswith("source/")}
        syncing = time.perf_counter()
        cache.synchronize(bundle, output, sources, lambda p: Path(p).relative_to("source"), backup, state.get("source_files"))
        state["source_files"] = sources
        toolchain_stamp = cache.prepare_tools(bundle, output, manifest, backup)
        print(f"  Source/tool synchronization: {time.perf_counter() - syncing:.2f}s", flush=True)
        state["setup_timings"] = {"preflight": round(checked - started, 3),
                                  "source_tools": round(time.perf_counter() - syncing, 3)}
        save(marker, state)
        with (output / "compiler.log").open("a", encoding="utf-8") as log, contextlib.redirect_stdout(Tee(sys.stdout, log)):
            return build_stages(bundle, nup, output, jobs, resolution, unlock, state, manifest, before, toolchain_stamp, started)


def build_stages(bundle, nup, output, jobs, resolution, unlock, state, manifest, before, toolchain_stamp, total_started):
    started = time.perf_counter()
    timings = dict(state.get("setup_timings", {}))
    def done(stage, began):
        timings[stage] = round(time.perf_counter() - began, 3)
        state["timings"] = timings
        save(output / STATE, state)
        print(f"  {stage}: {timings[stage]:.2f}s", flush=True)
    try:
        exefs = output / "work/game/exefs"
        print("[2/6] Extracting and hashing executable modules...", flush=True)
        began = time.perf_counter()
        try:
            verified = hwaoi.verify(exefs)
            print("  Reusing verified executable modules.", flush=True)
        except (OSError, ValueError):
            cache.quarantine(output, exefs)
            extract_game_exefs(nup, exefs)
            verified = hwaoi.verify(exefs)
        done("exefs", began)
        print("[3/6] Preparing the read-only game resource cache...", flush=True)
        began = time.perf_counter()
        common = ["--exefs", exefs]
        romfs = output / "work/game/romfs"
        resource = cache.resource_cache(romfs, state["nup_identity"], verified["exefs_sha256"], nup, state.get("resource_cache"))
        if resource is None:
            cache.quarantine(output, romfs)
            run_tool(bundle, output, *common, "prepare-romfs", "--nup", nup)
            resource = cache.resource_cache(romfs, state["nup_identity"], verified["exefs_sha256"], nup, None, prepared=True)
            require(resource is not None, "Prepared resource cache failed validation")
        else:
            print("  Reusing verified resource cache.", flush=True)
        state["resource_cache"] = resource
        done("resources", began)
        print("[3/6] Installing DLC (add-on content)...", flush=True)
        began = time.perf_counter()
        installed = install_game_dlc(nup, output)
        state["dlc"] = sorted(installed)
        done("dlc", began)
        print("[4/6] Translating ARM64 game code to C...", flush=True)
        began = time.perf_counter()
        generated = output / "work/generated/hwaoi196608-full"
        generation_key = cache.generation_key(output, manifest, unlock)
        reuse = state.get("generation_key") == generation_key and state.get("generated_files")
        try:
            hwaoi.require_successful_generation(generated)
            reuse = reuse and cache.generated_files(generated) == state["generated_files"]
        except (OSError, ValueError):
            reuse = False
        if reuse:
            print("  Reusing verified generated C; generator and profile unchanged.", flush=True)
        else:
            if generated.exists() and not (generated / "hwaoi-input.json").is_file():
                cache.quarantine(output, generated)
            run_tool(bundle, output, *common, "generate", "--rate-unlock" if unlock else "--display-settings",
                     "--out", "work/generated/hwaoi196608-full", "--limit", "0", "--jobs", jobs)
            state["generation_key"] = generation_key
            state["generated_files"] = cache.generated_files(generated)
        done("generation", began)
        print("[5/6] Compiling and linking the native Windows game (this can take a while)...", flush=True)
        began = time.perf_counter()
        run_tool(bundle, output, *common, "build", "--romfs", "work/game/romfs", "--deps", output / "work/toolchain/deps",
                 "--toolchain", output / "work/toolchain/llvm", "--ninja", output / "work/toolchain/ninja.exe",
                 "--toolchain-stamp", toolchain_stamp, "--jobs", jobs)
        done("compile_link", began)
        print("[6/6] Installing the shader translator and launch settings...", flush=True)
        game = output / "work/build/hwaoi196608"
        delivery = {p: sha for p, sha in manifest["files"].items() if p.startswith("translator/") or p == "bin/zlib1.dll"}
        def destination(relative):
            return Path("work/build/hwaoi196608") / ("zlib1.dll" if relative == "bin/zlib1.dll" else
                Path("xlate-managed-irbanks") / Path(relative).relative_to("translator"))
        cache.synchronize(bundle, output, delivery, destination, Path("work/backups") / f"delivery-{time.time_ns()}",
                          state.get("delivery_files"))
        state["delivery_files"] = delivery
        if not (game / "hwaoi.ini").is_file():
            (game / "hwaoi.ini").write_text("[settings]\noutput_res=" + resolution +
                                           "\nrender_scale=100\nfps_limit=60\n", encoding="utf-8")
        else:
            lines = (game / "hwaoi.ini").read_text(encoding="utf-8").splitlines(keepends=True)
            found = False
            for i, line in enumerate(lines):
                if line.split("=", 1)[0].strip().lower() == "output_res" and "=" in line:
                    found = True
                    if line.split("=", 1)[1].strip() != resolution:
                        lines[i] = f"output_res={resolution}\n"
            if not found:
                lines.append(f"\n[display]\noutput_res={resolution}\n")
            cache.write_changed(game / "hwaoi.ini", "".join(lines).encode("utf-8"))
        after = nup.stat()
        require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), "Source NUP changed during build")
        state.update(status="complete", executable_sha256=hwaoi.sha256(game / "hwaoi.exe"))
        state["timings"]["stages_total"] = round(time.perf_counter() - started, 3)
        state["timings"]["total"] = round(time.perf_counter() - total_started, 3)
        save(output / STATE, state)
        print("BUILD COMPLETE. Use Open compiled game in the compiler window.\n"
              "Keep the NUP at its original location: the game reads resources from it.\n"
              "This local output contains game-derived files and is not for redistribution.", flush=True)
    except Exception:
        traceback.print_exc(file=sys.stdout)
        raise
    return output


def launch(bundle, output):
    bundle, output = Path(bundle).resolve(strict=True), Path(output).resolve(strict=True)
    bundle_check(bundle)
    state = json.loads((output / STATE).read_text(encoding="utf-8"))
    require(state.get("format") == "hwaoi-local-build-v1" and state.get("status") == "complete", "Build is incomplete")
    require(inspect_nup(state["nup"]) == state["nup_identity"], "Source NUP has moved or changed")
    game = output / "work/build/hwaoi196608/hwaoi.exe"
    require(hwaoi.sha256(game) == state["executable_sha256"], "Compiled executable has changed; rebuild in a fresh folder")
    env = clean_environment(bundle)
    env["HWAOI_RATE_UNLOCK"] = "1" if state["rate_unlock"] else "0"
    command = [str(bundle / "python/python.exe"), "-E", "-s", str(output / "tools/hwaoi.py"), "launch",
               "--zlib", str(game.parent / "zlib1.dll"), "--no-captures"]
    completed = subprocess.run(command, cwd=output, env=env)
    require(completed.returncode == 0, "Game launch failed; see the launch report under work/reports")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "launch", "check"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--nup", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--resolution", choices=RESOLUTIONS, default="1920x1080")
    parser.add_argument("--unlock", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "check":
            bundle_check(args.bundle)
            if args.nup:
                print(json.dumps(inspect_nup(args.nup)))
            print("Compiler bundle checks passed")
        elif args.action == "build":
            require(args.nup and args.output, "Choose a game NUP and output folder")
            build(args.bundle, args.nup, args.output, args.jobs, args.resolution, args.unlock)
        else:
            require(args.output, "Choose the completed output folder")
            launch(args.bundle, args.output)
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
