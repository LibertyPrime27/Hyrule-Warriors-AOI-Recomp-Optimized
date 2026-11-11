"""HWAoI-specific verified entry point on top of the imported DKB recompiler.

No Bananza patch table, installer or game resources are used. Generation and
object compilation are separate from linking/boot. All derived output is local.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config/games/hwaoi-update-196608.json"
DEFAULT_EXEFS = ROOT / "work/game/exefs"
DEFAULT_GENERATED = ROOT / "work/generated/hwaoi196608"
MODULES = ("main", "subsdk0", "sdk")
BUILD_NAME = "hwaoi196608"
GAME_EXE = "Hyrule Warriors AOI Recomp Optimized.exe"   # tools/build_game.py EXE
LEGACY_EXE = "hwaoi.exe"   # builds made before the rename


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def program_id(path):
    data = Path(path).read_bytes()
    need(len(data) >= 0x80 and data[:4] == b"META", "invalid main.npdm header")
    offset, size = struct.unpack_from("<II", data, 0x70)
    need(size >= 0x18 and offset <= len(data) and size <= len(data) - offset, "NPDM ACI0 outside file")
    need(data[offset:offset + 4] == b"ACI0", "NPDM has no ACI0 signature")
    return f"{struct.unpack_from('<Q', data, offset + 0x10)[0]:016X}"


def verify(exefs, profile_path=PROFILE):
    from hwrecomp.nso import Nso
    exefs = Path(exefs).resolve(strict=True)
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    need(profile["title_id"] == "04003F40232DE000", "not the HWAoI title profile")
    need(program_id(exefs / "main.npdm") == profile["title_id"], "main.npdm title ID mismatch")
    need(sha256(exefs / "main.npdm") == profile["npdm_sha256"], "main.npdm SHA-256 mismatch")
    module_details, hashes = [], {}
    for name, expected in profile["modules"].items():
        need(name in (*MODULES, "rtld"), f"unexpected module name: {name}")
        actual = sha256(exefs / name)
        need(actual == expected, f"{name}: SHA-256 mismatch; refusing a different game/build")
        module = Nso(exefs / name)  # independent segment hash and bounds validation
        hashes[name] = actual
        module_details.append({"name": name, "sha256": actual, "build_id": module.build_id,
                               "image_size": module.image_end})
    need(set(hashes) == {*MODULES, "rtld"}, "profile must contain all four observed modules")
    hashes["main.npdm"] = sha256(exefs / "main.npdm")
    return {"title_id": profile["title_id"], "ncm_version": profile["ncm_version"],
            "romfs_inventory": profile["romfs_inventory"],
            "display_version": profile.get("display_version"), "exefs": str(exefs),
            "exefs_sha256": hashes, "modules": module_details,
            "generated_modules": list(MODULES),
            "rtld_policy": "retained and hashed; DKB host linker replaces rtld execution (unverified for HWAoI)",
            "patches": "no inherited Bananza/TotK patches; fork profiles are selected during generation"}


def native_1080_profile(exefs):
    """Accept only the independently identified instructions in the exact main."""
    from hwrecomp.nso import Nso
    patch = ROOT / "recomp/patches-hwaoi196608-native1080.txt"
    module = Nso(Path(exefs) / "main")
    expected = json.loads(PROFILE.read_text(encoding="utf-8"))["modules"]["main"]
    need(module.sha256 == expected, "native 1080p profile: main SHA-256 mismatch")
    sites = 0
    for line in patch.read_text(encoding="utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if not fields or fields[0] == "set":
            continue
        need(6 <= len(fields) <= 7 and fields[:3] == ["site", "hwaoi_native1080", "main"],
             "native 1080p profile: unexpected patch entry")
        address, original = int(fields[3], 16), int(fields[4], 16)
        need(struct.unpack("<I", module.code(address, 4))[0] == original,
             f"native 1080p profile: instruction mismatch at main+{address:#x}")
        sites += 1
    need(sites == 12, "native 1080p profile must contain the twelve verified sites")
    return patch


def display_profile(exefs):
    """Configurable native sizes; never select this table for another module."""
    from hwrecomp.nso import Nso
    patch = ROOT / "recomp/patches-hwaoi196608-display.txt"
    module = Nso(Path(exefs) / "main")
    expected = json.loads(PROFILE.read_text(encoding="utf-8"))["modules"]["main"]
    need(module.sha256 == expected, "display profile: main SHA-256 mismatch")
    sites = set()
    for line in patch.read_text(encoding="utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if not fields or fields[0] == "set":
            continue
        need(6 <= len(fields) <= 8 and fields[:3] == ["site", "hwaoi_display", "main"],
             "display profile: unexpected patch entry")
        address, original = int(fields[3], 16), int(fields[4], 16)
        need(address not in sites, "display profile: duplicate site")
        need(struct.unpack("<I", module.code(address, 4))[0] == original,
             f"display profile: instruction mismatch at main+{address:#x}")
        sites.add(address)
    need(len(sites) == 33, "display profile must contain the 33 verified sites")
    return patch


def rate_profile(exefs):
    from hwrecomp.nso import Nso
    patch = ROOT / "recomp/patches-hwaoi196608-rate.txt"
    module = Nso(Path(exefs) / "main")
    expected = json.loads(PROFILE.read_text(encoding="utf-8"))["modules"]["main"]
    need(module.sha256 == expected, "rate profile: main SHA-256 mismatch")
    entries = [line.split("#", 1)[0].split() for line in patch.read_text(encoding="utf-8").splitlines()]
    sites = [f for f in entries if f and f[0] == "site"]
    expected_sites = {0x6c281c: ("hwaoi_rate", 0xb9403368, 15),
                      0x6c2b50: ("hwaoi_step", 0xbd403d00, 30),
                      0x6c2b80: ("hwaoi_step", 0x1e201820, 26)}
    need(len(sites) == len(expected_sites), "rate profile: expected three verified scheduler/step sites")
    seen = set()
    for site in sites:
        address, original = int(site[3], 16), int(site[4], 16)
        expected_site = expected_sites.get(address)
        need(expected_site is not None and address not in seen and
             (site[1], original, len(site)) == expected_site and site[2] == "main",
             "rate profile: unexpected scheduler/step site")
        need(struct.unpack("<I", module.code(address, 4))[0] == original,
             f"rate profile: scheduler/step instruction mismatch at main+{address:#x}")
        seen.add(address)
    return patch


def display_rate_bytes(exefs):
    paths = (display_profile(exefs), rate_profile(exefs))
    return ("\n".join(p.read_text(encoding="utf-8") for p in paths) + "\n").encode("utf-8")


def require_successful_generation(out):
    record = out / "hwaoi-generation.json"
    need(record.is_file(), "generation success record missing; generate before compiling")
    result = json.loads(record.read_text(encoding="utf-8"))
    need(result.get("status") == "pass" and result.get("input_sha256") == sha256(out / "hwaoi-input.json"),
         "generation is incomplete or belongs to different inputs; regenerate before compiling")


def local_output(path, kind):
    result = Path(path).resolve()
    boundary = (ROOT / "work" / kind).resolve()
    need(result != boundary and result.is_relative_to(boundary), f"output must be a child of {boundary}")
    return result


def save_json(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(command, **kwargs):
    print("+", subprocess.list2cmdline([str(x) for x in command]), flush=True)
    env = dict(os.environ)
    env['PYTHONUTF8'] = '1'
    completed = subprocess.run([str(x) for x in command], cwd=ROOT, env=env, **kwargs)
    need(completed.returncode == 0, f"command failed with exit code {completed.returncode}")
    return completed


def prepare_generated(out, identity):
    marker = out / "hwaoi-input.json"
    if out.exists():
        need(marker.is_file(), "refusing to reuse an unmarked generation directory")
        old = json.loads(marker.read_text(encoding="utf-8"))
        need(old["exefs_sha256"] == identity["exefs_sha256"], "generation directory belongs to another input")
    else:
        out.mkdir(parents=True)
    save_json(marker, identity)


def generation_summary(out, limit):
    summary = []
    for name in MODULES:
        manifest = json.loads((out / name / "manifest.json").read_text(encoding="utf-8"))
        stats = manifest["stats"]
        summary.append({"module": name, "functions_discovered": stats["functions"],
                        "functions_emitted": manifest["function_count"],
                        "instructions": stats["instructions"], "unsupported": stats["unsupported"],
                        "traps": stats["traps"], "c_bytes": stats["c_bytes"],
                        "unsupported_census": manifest["unsupported_census"]})
    return {"stage": "C generation only", "function_limit_per_module": limit, "modules": summary,
            "all_discovered_functions_emitted": all(m["functions_discovered"] == m["functions_emitted"] for m in summary),
            "runtime_verified": False, "playable": False}


def compile_smoke(generated, out, clang, chunks):
    need(1 <= chunks <= 8, "smoke chunks must be in 1..8 per module")
    need(not out.exists(), "smoke output exists; choose a new directory")
    need(Path(clang).is_file(), f"compiler not found: {clang}")
    out.mkdir(parents=True)
    report = {"stage": "selected native object compilation (no linking/execution)", "objects": []}
    for module in MODULES:
        manifest = json.loads((generated / module / "manifest.json").read_text(encoding="utf-8"))
        for filename in manifest["chunks"][:chunks]:
            need(Path(filename).name == filename, "invalid generated chunk path")
            source = generated / module / filename
            destination = out / f"{module}-{source.stem}.o"
            command = [clang, "-std=gnu11", "-O1", "-fno-strict-aliasing", "-Wno-return-type",
                       "-Wno-unused-label", "-march=x86-64-v3", "-I", ROOT / "runtime", "-I", generated,
                       "-c", source, "-o", destination]
            run(command, timeout=180)
            report["objects"].append({"source": str(source), "object": str(destination),
                                      "size": destination.stat().st_size, "sha256": sha256(destination)})
    save_json(out / "compile-report.json", report)
    return report


def audit(exefs, identity):
    sys.path.insert(0, str(ROOT / "recomp"))
    from nso import Nso
    from layout import layout
    available = set()
    for path in (ROOT / "runtime").glob("*.c"):
        available.update(re.findall(r'HOST_OVERRIDE\(\s*"([^"\n]+)"', path.read_text(encoding="utf-8")))
    modules = {name: Nso(str(exefs / name)) for name in (*MODULES, "rtld")}
    exports = {}
    for name in MODULES:
        for symbol, info, section, value, size in modules[name].exports():
            if symbol and info >> 4 in (1, 2):
                exports.setdefault(symbol, []).append(name)
    records = []
    for name in MODULES:
        module = modules[name]
        imports = [{"name": s[0], "host_override_declared": s[0] in available,
                    "guest_export_providers": exports.get(s[0], []), "weak": s[1] >> 4 == 2}
                   for s in module.imports()]
        records.append({"module": name, "symbol_count": len(module.syms), "relocation_count": len(module.relocs),
                        "needed": module.needed, "imports": imports})
    return {**identity, "proposed_layout": [{"name": n, "base": hex(b), "size": s} for n, b, s in layout(exefs)],
            "linkage_inventory": records,
            "limitations": ["Textual host declarations are candidates, not verified ABI/behavior",
                            "Weak/global visibility, relocation execution and OS/GPU behavior not validated",
                            "No game boot performed"]}


def scene_sharpness(value):
    """Experimental strength of the verified 3D RCAS pass, or its original parameters."""
    if value == "game":
        return value
    if value.isascii() and value.isdecimal() and 0 <= int(value) <= 100:
        return str(int(value))
    raise argparse.ArgumentTypeError("scene sharpness must be game or an integer from 0 to 100")


def launch_interactive(args, identity):
    """Explicit user trial; retains logs and uses the user's local save profile."""
    need(os.name == "nt", "interactive launcher currently requires Windows")
    exe = local_output(args.exe, "build")
    need(exe.is_file(), f"runtime not built: {exe}")
    translator = exe.parent / "xlate-managed-irbanks/totk_xlate.exe"
    need(translator.is_file() and translator.with_suffix(".id").is_file(), "IR-bank shader translator is missing")
    zlib = args.zlib.resolve(strict=True)
    need(zlib.is_file(), "native zlib DLL is missing")
    report_dir = local_output(ROOT / "work/reports" / datetime.now().strftime("interactive-%Y%m%d-%H%M%S-%f"), "reports")
    report_dir.mkdir(parents=True)
    env = dict(os.environ)
    # Environment overrides take precedence over inherited ini/probe settings.
    options = {"HWAOI_GPU": "vulkan", "HWAOI_FULLSCREEN": "0", "HWAOI_WINDOW_SIZE": "1280x720",
               "HWAOI_INPUT": args.input_mode, "HWAOI_RUMBLE": "0", "HWAOI_PROGRESS": "1",
               "HWAOI_EXEFS": str(args.exefs.resolve()),
               "HWAOI_PROFILE_DIR": str(local_output(args.profile_dir, "saves")),
               "HWAOI_GSV_LOCAL_FRAME_POLICY": "1", "HWAOI_NATIVE_INFLATE": "1",
               "HWAOI_ZLIB_DLL": str(zlib), "HWAOI_POOL_BACKPRESSURE": "1", "HWAOI_POOL_DIAG": "0",
               "HWAOI_IR_BANKS": "1", "HWAOI_XLATE": str(translator), "HWAOI_TRACE": "sched,perf",
               "HWAOI_SHOTS": "60-1800:60", "HWAOI_SHOTS_DIR": str(report_dir / "presents"),
               "HWAOI_SHOTS_FULL": "1", "HWAOI_CORE_STALL_DIAG": "0",
               "DKB_SHADER_FAILURE_DUMP": str(report_dir / "shader-failures")}
    options.update(pipeline_options(diagnostic=args.input_feed is not None or args.input_script is not None,
                                    force_sync=getattr(args, "pipe_sync", False)))
    for setting in ("INPUT_SCRIPT", "INPUT_RECORD", "INPUT_FEED", "XLATE_ID", "PIPE_SYNC", "PIPE_SKIP", "PIPE_PREBUILD"):
        for prefix in ("HWAOI_", "DKB_", "TOTK_"):
            env.pop(prefix + setting, None)
    env.update(options)
    if args.resolution is not None:
        record_path = exe.parent / "hwaoi-build.json"
        built = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
        need(built.get("sha256") == sha256(exe) and built.get("exefs_sha256") == identity["exefs_sha256"]
             and (built.get("code_patch_profile") or {}).get("name") in ("hwaoi196608-display", "hwaoi196608-display-rate"),
             "--resolution requires a matching display-settings build")
        options["HWAOI_OUTPUT_RES"] = env["HWAOI_OUTPUT_RES"] = args.resolution
    if args.fps is not None:
        need(int(args.fps) <= 60 or args.experimental_rate_control,
             "90/120 FPS are still experimental; use --experimental-rate-control with a diagnostic feed")
        options["HWAOI_FPS_LIMIT"] = env["HWAOI_FPS_LIMIT"] = args.fps
    if args.scene_sharpness is not None:
        options["HWAOI_SCENE_SHARPNESS"] = env["HWAOI_SCENE_SHARPNESS"] = args.scene_sharpness
    # Retain selected inherited diagnostics in the private launch report.
    for key in ("HWAOI_OUTPUT_RES", "HWAOI_SCENE_RES", "HWAOI_FPS_LIMIT", "HWAOI_EXPERIMENTAL_RATE", "HWAOI_APP_POOL", "HWAOI_VK_PRESENT", "HWAOI_DISPLAY_POOL_TEST",
                "HWAOI_GPU_WRITE_DIAG", "HWAOI_GPU_READBACK_DIAG", "HWAOI_GPU_QUADS", "HWAOI_INDIRECT_BATCH", "HWAOI_UI_DRAW_DIAG", "HWAOI_RUNTIME_MEMORY_DIAG", "HWAOI_RATE_DIAG", "HWAOI_RATE_UNLOCK", "HWAOI_VK_VALIDATION",
                "VK_LAYER_PATH", "HWAOI_DUMP", "HWAOI_DUMP_DIR", "HWAOI_DUMP_LOG", "HWAOI_DUMP_UBO", "HWAOI_DUMP_TRIGGER",
                "HWAOI_DUMP_TEX", "HWAOI_DUMP_SKIP_RUNS", "HWAOI_DUMP_METADATA", "HWAOI_SCENE_SHARPNESS", "HWAOI_NATIVE_1080",
                "DKB_SHADER_DUMP"):
        if key in env:
            options[key] = env[key]
    if args.input_feed is not None:
        feed = local_output(args.input_feed, "reports").resolve(strict=True)
        need(feed.is_file(), "diagnostic input feed must be a local file under work/reports")
        options["HWAOI_INPUT_FEED"] = env["HWAOI_INPUT_FEED"] = str(feed)
        # Long diagnostic progression needs observations after the startup clip.
        # Keep them at 480x270 instead of writing full-size images each second.
        options["HWAOI_SHOTS"] = env["HWAOI_SHOTS"] = "120-1000000000:120"
        options["HWAOI_SHOTS_LATEST"] = env["HWAOI_SHOTS_LATEST"] = "1"
        for prefix in ("HWAOI_", "DKB_", "TOTK_"):
            env.pop(prefix + "SHOTS_FULL", None)
        options.pop("HWAOI_SHOTS_FULL", None)
        # The diagnostic controller can resize through cfg_set; forced environment
        # values would take precedence over those live requests.
        options.pop("HWAOI_WINDOW_SIZE", None)
        for prefix in ("HWAOI_", "DKB_", "TOTK_"):
            env.pop(prefix + "WINDOW_SIZE", None)
    if args.experimental_rate_control:
        need(args.input_feed is not None, "experimental rate control requires an explicit diagnostic input feed")
        options["HWAOI_EXPERIMENTAL_RATE"] = env["HWAOI_EXPERIMENTAL_RATE"] = "1"
    if args.input_script is not None:
        script = local_output(args.input_script, "reports").resolve(strict=True)
        need(script.is_file(), "diagnostic input script must be a local file under work/reports")
        options["HWAOI_INPUT_SCRIPT"] = env["HWAOI_INPUT_SCRIPT"] = str(script)
    if args.no_captures:
        for key in ("HWAOI_SHOTS", "HWAOI_SHOTS_DIR", "HWAOI_SHOTS_FULL", "HWAOI_SHOTS_LATEST"):
            options.pop(key, None)
            env.pop(key, None)
    report = {"stage": "interactive experimental launch", "executable": str(exe), "sha256": sha256(exe),
              "exefs_sha256": identity["exefs_sha256"], "runtime_options": options,
              "dependencies": {"translator": sha256(translator), "zlib": sha256(zlib)},
              "arguments": ["--play-experimental"], "report_directory": str(report_dir),
              "gameplay_verified": False}
    build_record = exe.parent / "hwaoi-build.json"
    if build_record.is_file():
        built = json.loads(build_record.read_text(encoding="utf-8"))
        if built.get("sha256") == report["sha256"] and built.get("exefs_sha256") == identity["exefs_sha256"]:
            report["code_patch_profile"] = built.get("code_patch_profile")
    with (report_dir / "stdout.log").open("wb") as stdout, (report_dir / "stderr.log").open("wb") as stderr:
        proc = subprocess.Popen([str(exe), "--play-experimental"], cwd=exe.parent, env=env,
                                stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
    report["pid"] = proc.pid
    save_json(report_dir / "launch.json", report)
    return report


def pipeline_options(diagnostic, force_sync):
    """Vulkan pipeline policy for an interactive launch.

    Synchronous builds (HWAOI_PIPE_SYNC=1) compile every first-seen pipeline on the drawing thread: the first title or
    field scene then presents nothing for 10+ s and recorded pipelines are never prewarmed at the next boot. Normal
    play uses the inherited background builder instead, with boot prewarm from pipeline-keys.bin, and skips only draws
    into passes whose targets were all rendered the previous frame (strict); one-shot targets still wait for their
    pipeline. Diagnostic scripts/feeds and --pipe-sync keep the deterministic synchronous mode that probes use.
    """
    if diagnostic or force_sync:
        return {"HWAOI_PIPE_SYNC": "1"}
    return {"HWAOI_PIPE_SYNC": "0", "HWAOI_PIPE_SKIP": "strict", "HWAOI_PIPE_PREBUILD": "1"}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return dispatch(args)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exefs", type=Path, default=DEFAULT_EXEFS)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("verify", help="Check title ID, exact module hashes and NSO segment hashes")
    inspect = sub.add_parser("audit", help="Inspect imports/relocations and proposed DKB module layout")
    inspect.add_argument("--report", type=Path, default=ROOT / "work/reports/hwaoi-linkage.json")
    romfs = sub.add_parser("prepare-romfs", help="Prepare verified compressed cache; raw resources stay in the NUP")
    romfs.add_argument("--nup", type=Path, required=True)
    romfs.add_argument("--update-nup", type=Path, help="separate update NUP for a base-only game NUP (optional)")
    romfs.add_argument("--out", type=Path, default=ROOT / "work/game/romfs")
    gen = sub.add_parser("generate", help="Run the generator with verified HWAoI inputs and optional fork profiles")
    gen.add_argument("--out", type=Path, default=DEFAULT_GENERATED)
    gen.add_argument("--limit", type=int, default=256, help="Functions per module; 0 explicitly requests all")
    gen.add_argument("--jobs", type=int, default=0, help="0 selects workers from available RAM/commit; 1..8 sets a ceiling")
    gen.add_argument("--native-1080", action="store_true", help="select the exact-build HWAoI native 1080p profile")
    gen.add_argument("--display-settings", action="store_true", help="select exact-build native resolution settings (720p..4K)")
    gen.add_argument("--rate-unlock", action="store_true", help="generate the experimental exact-build native scheduler-rate profile with display settings")
    smoke = sub.add_parser("compile-smoke", help="Compile selected generated chunks, without claiming a game build")
    smoke.add_argument("--generated", type=Path, default=DEFAULT_GENERATED)
    smoke.add_argument("--out", type=Path, default=ROOT / "work/build/hwaoi-smoke")
    smoke.add_argument("--clang", type=Path, default=Path("F:/llvm-mingw/bin/clang.exe"))
    smoke.add_argument("--chunks", type=int, default=1)
    build = sub.add_parser("build", help="Link a complete generation with explicit runtime dependencies (experimental)")
    build.add_argument("--generated", type=Path, default=ROOT / "work/generated/hwaoi196608-full")
    build.add_argument("--out", type=Path, default=ROOT / "work/build/hwaoi196608")
    build.add_argument("--romfs", type=Path, required=True, help="Prepared romfs.bin/index.json directory; not the NUP")
    build.add_argument("--deps", type=Path, required=True, help="Existing include/lib/bin runtime dependency tree")
    build.add_argument("--toolchain", type=Path, default=Path("F:/llvm-mingw"))
    build.add_argument("--ninja", type=Path, default=Path("F:/Ninja/ninja.exe"))
    build.add_argument("--jobs", type=int, default=0, help="0 selects workers from available RAM/commit; 1..8 sets a ceiling")
    build.add_argument("--toolchain-stamp", type=Path, help="compiler/dependency identity for incremental builds")
    build.add_argument("--icon", type=Path, help="the game's .ico (tools/hwaoi_icon.py) to embed in the executable")
    launch = sub.add_parser("launch", help="Open the experimental game with live input and local logs")
    launch.add_argument("--exe", type=Path, default=ROOT / "work/build/hwaoi196608" / GAME_EXE)
    launch.add_argument("--zlib", type=Path, default=Path("F:/GCC/ucrt64/bin/zlib1.dll"))
    launch.add_argument("--input-feed", type=Path, help="opt-in expiring diagnostic controls under work/reports")
    launch.add_argument("--input", dest="input_mode", choices=("auto", "keyboard", "none"), default="auto", help="physical input policy; diagnostic scripts/feed remain available")
    launch.add_argument("--no-captures", action="store_true", help="disable automatic diagnostic screenshots for normal play")
    launch.add_argument("--scene-sharpness", type=scene_sharpness, help="experimental 3D sharpening strength 0..100, or game for original parameters (default)")
    launch.add_argument("--experimental-rate-control", action="store_true", help="opt-in unqualified 30/60/90/120 presentation-rate experiments")
    launch.add_argument("--resolution", choices=("1280x720", "1920x1080", "2560x1440", "3840x2160"), help="native game resolution; non-1080p modes remain experimental; requires display-settings generation")
    launch.add_argument("--fps", choices=("30", "60", "90", "120"), help="presentation target; 90/120 remain experimental")
    launch.add_argument("--input-script", type=Path, help="opt-in startup diagnostic script under work/reports")
    launch.add_argument("--profile-dir", type=Path, default=ROOT / "work/saves/hwaoi196608", help="local save profile under work/saves")
    launch.add_argument("--pipe-sync", action="store_true",
                        help="build every Vulkan pipeline on the drawing thread (old behaviour: long black stalls on new scenes)")
    return parser


def dispatch(args):
    try:
        identity = verify(args.exefs)
        if args.action == "verify":
            report = identity
        elif args.action == "launch":
            report = launch_interactive(args, identity)
        elif args.action == "prepare-romfs":
            from hwaoi_romfs import prepare
            report = prepare(args.nup, local_output(args.out, "game"), identity, update_path=args.update_nup)
        elif args.action == "audit":
            report = audit(args.exefs, identity)
            destination = local_output(args.report, "reports")
            save_json(destination, report)
            report = {"report": str(destination), "proposed_layout": report["proposed_layout"],
                      "modules": [{"module": m["module"], "symbols": m["symbol_count"],
                                   "relocations": m["relocation_count"], "imports": len(m["imports"])}
                                  for m in report["linkage_inventory"]], "runtime_verified": False}
        elif args.action == "generate":
            need(0 <= args.limit <= 1000000 and 0 <= args.jobs <= 8, "invalid limit/jobs")
            out = local_output(args.out, "generated")
            need(not (args.native_1080 and (args.display_settings or args.rate_unlock)), "select one code profile")
            patch = display_profile(args.exefs) if args.display_settings else native_1080_profile(args.exefs) if args.native_1080 else None
            profile_name = "hwaoi196608-display" if args.display_settings else "hwaoi196608-native1080"
            combined = display_rate_bytes(args.exefs) if args.rate_unlock else None
            if combined is not None:
                profile_name = "hwaoi196608-display-rate"
                patch = out / "hwaoi-display-rate.txt"
            identity["code_patch_profile"] = ({"name": profile_name, "sha256": hashlib.sha256(combined).hexdigest() if combined is not None else sha256(patch)}
                                               if patch else None)
            prepare_generated(out, identity)
            save_json(out / "hwaoi-generation.json", {"status": "generating", "input_sha256": sha256(out / "hwaoi-input.json")})
            if combined is not None:
                patch.write_bytes(combined)
            command = [sys.executable, ROOT / "recomp/gen.py", "--exefs", args.exefs.resolve(), "--out", out,
                       "--modules", ",".join(MODULES), "--jobs", args.jobs, "--limit", args.limit]
            if patch:
                command += ["--patches", patch]
            run(command)
            report = generation_summary(out, args.limit)
            report.update(status="pass", input_sha256=sha256(out / "hwaoi-input.json"))
            save_json(out / "hwaoi-generation.json", report)
            save_json(out / "expected.json", identity)
        elif args.action in ("compile-smoke", "build"):
            generated = local_output(args.generated, "generated")
            require_successful_generation(generated)
            old = json.loads((generated / "hwaoi-input.json").read_text(encoding="utf-8"))
            need(old["exefs_sha256"] == identity["exefs_sha256"], "generated input does not match current modules")
            selected_profile = old.get("code_patch_profile")
            if selected_profile:
                profiles = {"hwaoi196608-native1080": native_1080_profile, "hwaoi196608-display": display_profile,
                            "hwaoi196608-display-rate": None}
                need(selected_profile.get("name") in profiles, "unknown generated code profile")
                if selected_profile["name"] == "hwaoi196608-display-rate":
                    combined = display_rate_bytes(args.exefs)
                    patch = generated / "hwaoi-display-rate.txt"
                    need(patch.is_file() and patch.read_bytes() == combined, "native rate profile changed; regenerate first")
                else:
                    patch = profiles[selected_profile["name"]](args.exefs)
                need(selected_profile.get("sha256") == sha256(patch),
                     "native 1080p profile changed since generation; regenerate before building")
            if args.action == "compile-smoke":
                report = compile_smoke(generated, local_output(args.out, "build"), args.clang, args.chunks)
            else:
                complete = generation_summary(generated, 0)
                need(complete["all_discovered_functions_emitted"], "partial generation cannot be linked as a full game")
                need(all(m["unsupported"] == 0 for m in complete["modules"]), "unsupported instructions remain")
                need((args.romfs / "index.json").is_file(), "prepared HWAoI RomFS index is required")
                storage = json.loads((args.romfs / "index.json").read_text(encoding="utf-8"))
                need(storage.get("status") == "pass", "RomFS preparation is incomplete")
                need(storage.get("exefs_sha256") == identity["exefs_sha256"], "RomFS belongs to another ExeFS build")
                need((args.romfs / ("cache.bin" if storage.get("storage") == "nup-cache-v1" else "romfs.bin")).is_file(),
                     "prepared HWAoI RomFS storage is missing")
                need(args.deps.is_dir() and args.ninja.is_file(), "runtime dependency tree or Ninja missing")
                need(0 <= args.jobs <= 8, "build jobs must be 0 (automatic) or in 1..8")
                out = local_output(args.out, "build")
                command = [sys.executable, ROOT / "tools/build_game.py", "--version", BUILD_NAME,
                     "--gen", generated, "--out", out, "--expected", generated / "expected.json",
                     "--exefs", args.exefs.resolve(), "--romfs", args.romfs.resolve(),
                     "--profile", ROOT / "work/saves/hwaoi196608", "--toolchain", args.toolchain,
                     "--ninja", args.ninja, "--deps", args.deps, "--jobs", args.jobs, "--full-build"]
                if args.toolchain_stamp:
                    need(args.toolchain_stamp.is_file(), "toolchain identity stamp is missing")
                    command += ["--toolchain-stamp", args.toolchain_stamp.resolve()]
                if args.icon:
                    need(args.icon.is_file(), "game icon is missing")
                    command += ["--icon", args.icon.resolve()]
                run(command)
                report = {"stage": "linked experimental runtime", "output": str(out),
                          "game_boot_enabled": False, "experimental_interactive_command": "python tools/hwaoi.py launch",
                          "playable": False, "sha256": sha256(out / GAME_EXE),
                          "exefs_sha256": identity["exefs_sha256"], "code_patch_profile": selected_profile}
                save_json(out / "hwaoi-build.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, ImportError, subprocess.TimeoutExpired) as error:
        print(f"hwaoi: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
