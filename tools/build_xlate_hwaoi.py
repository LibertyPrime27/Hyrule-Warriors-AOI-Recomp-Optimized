"""Build HWAoI's source-identified managed shader translator without MSVC.

Uses the imported Ryujinx sources and the same selected patches as
tools/build_xlate.py. Vendor files are never modified. Managed self-contained
publishing avoids NativeAOT's MSVC requirement; the entire publish directory is
needed, not only totk_xlate.exe. Output stays under ignored work/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

import build_xlate as upstream
from xlate_smoke import fragment_code, mov32i, SCHED, EXIT

ROOT = Path(__file__).resolve().parents[1]
MODE = "managed-self-contained-win-x64"
VERSION = 2


def hwaoi_patches():
    """Extra fork patches are separate from the fixed upstream selection."""
    return sorted((ROOT / "tools/xlate/hwaoi-patches").glob("*.patch"))


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def target_path(path, boundary):
    path, boundary = Path(path).resolve(), Path(boundary).resolve()
    require(path != boundary and path.is_relative_to(boundary), f"output must be below {boundary}")
    return path


def hashes(root):
    return {path.relative_to(root).as_posix(): digest(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def run_logged(command, cwd, env, log):
    print("+", subprocess.list2cmdline([str(x) for x in command]), flush=True)
    with log.open("wb") as stream:
        result = subprocess.run([str(x) for x in command], cwd=cwd, env=env,
                                stdout=stream, stderr=subprocess.STDOUT, timeout=600)
    if result.returncode:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
        raise RuntimeError(f"command exit {result.returncode}; log {log}\n" + "\n".join(tail))


def check_vendor():
    """Verify the preserved import against its separately pinned reference clone."""
    metadata = json.loads((ROOT / "config/upstream.json").read_text(encoding="utf-8"))
    reference = ROOT / "work/references/dkb-pc-recomp"
    require((reference / ".git").exists(), "pinned DKB source checkout missing; cannot verify vendor provenance")
    revision = subprocess.run(["git", "-C", str(reference), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    require(revision == metadata["commit"], "DKB reference checkout differs from the recorded source import")
    ours = upstream.tree_hashes(upstream.VENDOR)
    theirs = upstream.tree_hashes(reference / "tools/vendor/ryujinx")
    require(ours == theirs, "vendor tree differs from the pinned DKB source reference")
    return {"reference_commit": revision, "files": len(ours),
            "tree_identity": hashlib.sha256(json.dumps(ours, sort_keys=True).encode()).hexdigest()}


def smoke(executable, folder, env, spirv_val=None):
    folder.mkdir()
    c0 = subprocess.run([str(executable), "--probe-nvn2-c0"], env=env, capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=30)
    (folder / "c0.stdout.log").write_text(c0.stdout, encoding="utf-8")
    (folder / "c0.stderr.log").write_text(c0.stderr, encoding="utf-8")
    require(c0.returncode == 0 and "passed" in c0.stdout, "NVN2 root-layout synthetic probe failed")
    code = folder / "constant-fragment.bin"
    code.write_bytes(fragment_code())  # Original upstream hand-assembled synthetic fixture, no game input.
    output = folder / "translated"
    job = {"key": "synthetic_constant", "stage": "ps", "code": str(code), "debug": False,
           "fout": {"0": "float"}}
    requests = [json.dumps({"job": job, "out": str(output)}), "invalid-json",
                json.dumps({"job": dict(job, key="after_error"), "out": str(output)})]
    result = subprocess.run([str(executable), "--serve"], input="\n".join(requests) + "\n",
                            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    (folder / "serve.stdout.log").write_text(result.stdout, encoding="utf-8")
    (folder / "serve.stderr.log").write_text(result.stderr, encoding="utf-8")
    replies = result.stdout.splitlines()
    require(result.returncode == 0 and len(replies) == 3 and replies[0] == "ok synthetic_constant"
            and replies[1].startswith("fail ? ") and replies[2] == "ok after_error",
            f"shader service smoke failed: {replies}")
    artifacts = {}
    for key in ("synthetic_constant", "after_error"):
        spv = output / f"{key}.spv"
        data = spv.read_bytes()
        require(len(data) >= 20 and len(data) % 4 == 0 and struct.unpack_from("<I", data)[0] == 0x07230203,
                "shader output has invalid SPIR-V framing")
        reflection = output / f"{key}.res.json"
        json.loads(reflection.read_text(encoding="utf-8"))
        artifacts[key] = {"spv_bytes": len(data), "spv_sha256": digest(spv),
                          "reflection_sha256": digest(reflection)}
    validation = "not run; no SPIR-V validator supplied"
    if spirv_val:
        # Some Windows SPIRV-Tools builds crash on non-ASCII argv paths. A relative
        # fixture name with a Unicode-capable subprocess cwd avoids that boundary.
        checked = subprocess.run([str(spirv_val.resolve()), "--target-env", "vulkan1.3", "synthetic_constant.spv"],
                                 cwd=output, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        (folder / "spirv-val.log").write_text(checked.stdout + checked.stderr, encoding="utf-8")
        require(checked.returncode == 0, "spirv-val rejected the synthetic output")
        validation = "passed"
    banks = smoke_ir_banks(executable, folder, env, spirv_val) if hwaoi_patches() else None
    typed_storage = smoke_typed_storage_address(executable, folder, env) if hwaoi_patches() else None
    return {"nvn2_c0": c0.stdout.strip(), "service_replies": replies, "artifacts": artifacts,
            "spirv_validation": validation, "private_ir_banks": banks,
            "typed_storage_address": typed_storage, "game_shaders_tested": False}


def smoke_typed_storage_address(executable, folder, env):
    """Required CPU-only certificate/refusal and independent arithmetic checks."""
    probe = subprocess.run([str(executable), "--probe-hwaoi-typed-storage-address"], env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    (folder / "typed-storage-address-probe.log").write_text(probe.stdout + probe.stderr, encoding="utf-8")
    prefix = "typed storage address probe: "
    output = probe.stdout.strip()
    count = output.removeprefix(prefix).removesuffix(" checks")
    require(probe.returncode == 0 and output.startswith(prefix) and output.endswith(" checks")
            and count.isdecimal() and int(count) >= 8192,
            "typed storage address semantic/provenance probe failed")
    return {"checks": int(count), "output": output, "gpu_execution": False}


def smoke_ir_banks(executable, folder, env, spirv_val):
    probe = subprocess.run([str(executable), "--probe-hwaoi-ir-banks"], env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
    (folder / "irbanks-probe.log").write_text(probe.stdout + probe.stderr, encoding="utf-8")
    require(probe.returncode == 0 and "passed" in probe.stdout, "private bank/SSA/live-mask probe failed")
    def move(db, dst, sb, src):
        return 0x5C98A00000070000 | dst | (db << 8) | (sb << 10) | (src << 20)
    instructions = [mov32i(0, 1.0), move(1, 255, 0, 0), mov32i(0, 0.0),
                    move(2, 255, 1, 255), move(0, 0, 2, 255),
                    mov32i(1, 0.5), mov32i(2, 0.25), mov32i(3, 1.0), EXIT]
    words = []
    for i, instruction in enumerate(instructions):
        if i % 3 == 0:
            words.append(SCHED)
        words.append(instruction)
    code = folder / "banked-fragment.bin"
    code.write_bytes(fragment_code()[:80] + struct.pack(f"<{len(words)}Q", *words))
    output = folder / "banked-output"
    job = {"key": "banked", "stage": "ps", "code": str(code), "debug": False, "fout": {"0": "float"}}
    requests = [dict(job, key="denied"), dict(job, key="banked", hwaoi_ir_banks=1),
                dict(job, key="wrong_version", hwaoi_ir_banks=2), dict(job, key="again_banked", hwaoi_ir_banks=1)]
    result = subprocess.run([str(executable), "--serve"],
                            input="\n".join(json.dumps({"job": q, "out": str(output)}) for q in requests) + "\n",
                            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    (folder / "irbanks-serve.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    replies = result.stdout.splitlines()
    require(result.returncode == 0 and len(replies) == 4 and replies[0].startswith("fail denied ")
            and replies[1] == "ok banked" and replies[2].startswith("fail wrong_version ")
            and replies[3] == "ok again_banked", f"private MOV feature gating failed: {replies}")
    spv = output / "banked.spv"
    require(spv.is_file() and spv.stat().st_size >= 20, "private MOV translation produced no SPIR-V")
    validation = "not run; no validator supplied"
    if spirv_val:
        checked = subprocess.run([str(spirv_val.resolve()), "--target-env", "vulkan1.3", spv.name],
                                 cwd=spv.parent, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        (folder / "irbanks-spirv-val.log").write_text(checked.stdout + checked.stderr, encoding="utf-8")
        require(checked.returncode == 0, "private MOV SPIR-V validation failed")
        validation = "passed"
    return {"probe": probe.stdout.strip(), "replies": replies, "spv_sha256": digest(spv),
            "spirv_validation": validation, "gpu_execution": False}


def build(args):
    require(os.name == "nt", "this build entry currently targets Windows x64")
    dotnet = args.dotnet.resolve(strict=True)
    output = target_path(args.out, ROOT / "work/build/hwaoi196608")
    require(output.name.startswith("xlate"), "translator deployment folder must start with xlate")
    cache = dotnet.parent.parent / "NuGet"   # beside the .NET folder (HWAOI_TOOLS, default F:/)
    for folder in (cache / "packages", cache / "http", cache / "plugins", cache / "cli-home", cache / "empty-feed"):
        folder.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"NUGET_PACKAGES": str(cache / "packages"), "NUGET_HTTP_CACHE_PATH": str(cache / "http"),
                "NUGET_PLUGINS_CACHE_PATH": str(cache / "plugins"), "DOTNET_CLI_HOME": str(cache / "cli-home"),
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
                "DOTNET_NOLOGO": "1", "DOTNET_MULTILEVEL_LOOKUP": "0", "DOTNET_ROOT": str(dotnet.parent)})
    sdk = subprocess.run([str(dotnet), "--version"], env=env, check=True, capture_output=True,
                         text=True, encoding="utf-8", timeout=30).stdout.strip()
    provenance = check_vendor()
    inputs = {"schema": VERSION, "mode": MODE, "sdk_version": sdk,
              "upstream_source_identity": upstream.identity(defined_float_minmax=args.defined_float_minmax),
              "builder_sha256": digest(Path(__file__)), "smoke_source_sha256": digest(ROOT / "tools/xlate_smoke.py"),
              "typed_storage_probe_sha256": digest(ROOT / "tests/typed_storage_address_probe.cs"),
              "defined_float_minmax": args.defined_float_minmax, "vendor": provenance,
              "hwaoi_patches": [{"name": p.name, "sha256": digest(p)} for p in hwaoi_patches()]}
    identity = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    workspace = target_path(ROOT / f"work/toolchain/xlate-hwaoi-{identity[:16]}", ROOT / "work/toolchain")
    require(not workspace.exists(), f"build workspace already exists (preserved): {workspace}")
    require(not output.exists(), f"deployment already exists (preserved): {output}")
    workspace.mkdir(parents=True)
    write_json(workspace / "inputs.json", dict(inputs, identity=identity, status="incomplete"))
    source = workspace / "ryujinx"
    for relative in upstream.tree_hashes(upstream.VENDOR):
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(upstream.VENDOR / relative, destination)
    for patch in upstream.selected_patches(defined_float_minmax=args.defined_float_minmax):
        for extra, label in ((["--check"], "check"), ([], "apply")):
            run_logged(["git", "apply", *extra, "--whitespace=nowarn", "--directory", "ryujinx", patch],
                       workspace, env, workspace / f"{patch.stem}-{label}.log")
    project = workspace / "xlate"
    project.mkdir()
    for name in ("Program.cs", "totk_xlate.csproj"):
        shutil.copy2(upstream.XLATE / name, project / name)
    # This original CPU-only probe is compiled into the fork's translator and
    # required by smoke before a successful deployment can be reported.
    shutil.copy2(ROOT / "tests/typed_storage_address_probe.cs", project / "typed_storage_address_probe.cs")
    for patch in hwaoi_patches():
        for extra, label in ((["--check"], "check"), ([], "apply")):
            log = workspace / f"hwaoi-{patch.stem}-{label}.log"
            # Multi-tree patches already contain ryujinx/ and xlate/. From an
            # ignored subdirectory git can silently skip all paths; anchor the
            # explicit destination at the repository root and verify delivery.
            run_logged(["git", "apply", *extra, "--verbose", "--whitespace=nowarn", "--directory",
                        workspace.relative_to(ROOT).as_posix(), patch], ROOT, env, log)
            require("Skipped patch" not in log.read_text(encoding="utf-8", errors="replace"),
                    f"fork patch was skipped: {patch.name}")
    if hwaoi_patches():
        require((source / "src/Ryujinx.Graphics.Shader/Translation/HwaoiPrivateRegisters.cs").is_file()
                and "--probe-hwaoi-ir-banks" in (project / "Program.cs").read_text(encoding="utf-8"),
                "fork patch files missing after apply")
    publish = workspace / "publish"
    properties = ["-p:PublishAot=false", "-p:SelfContained=true", "-p:UseAppHost=true",
                  "-p:InvariantGlobalization=true", "-p:GeneratePackageOnBuild=false",
                  "-p:NuGetAudit=false", f"-p:RyuSrc={source}", f"-p:RestoreFallbackFolders={(dotnet.parent / 'nuget').as_posix()}"]
    # Offline first: use already cached, version-matched NuGet packages. --online
    # explicitly permits nuget.org if a dependency/runtime pack is absent.
    restore_sources = "https://api.nuget.org/v3/index.json" if args.online else str(cache / "empty-feed")
    started = time.monotonic()
    run_logged([dotnet, "restore", project / "totk_xlate.csproj", "-r", "win-x64", *properties,
                f"-p:RestoreSources={restore_sources}"], workspace, env, workspace / "restore.log")
    run_logged([dotnet, "publish", project / "totk_xlate.csproj", "-c", "Release", "-r", "win-x64",
                "--no-restore", *properties, "-o", publish], workspace, env, workspace / "publish.log")
    executable = publish / "totk_xlate.exe"
    require(executable.is_file() and (publish / "totk_xlate.runtimeconfig.json").is_file(), "managed apphost missing")
    proof = smoke(executable, workspace / "smoke", env, args.spirv_val)
    executable.with_suffix(".id").write_text(identity + "\n", encoding="ascii")
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(publish, output)
    manifest = {"status": "pass", "identity": identity, "inputs": inputs, "mode": MODE,
                "dotnet": str(dotnet), "sdk_version": sdk, "workspace": str(workspace),
                "deployment": str(output), "executable": str(output / "totk_xlate.exe"),
                "deployment_files": hashes(output), "smoke": proof,
                "seconds": round(time.monotonic() - started, 3),
                "runtime_configuration": {"HWAOI_XLATE": str(output / "totk_xlate.exe")},
                "limits": ["Managed self-contained build, not NativeAOT", "No game shader or GPU test performed",
                           "The complete deployment folder is required"]}
    write_json(workspace / "build-report.json", manifest)
    write_json(output / "build-report.json", manifest)
    return {"identity": identity, "mode": MODE, "executable": manifest["executable"],
            "report": str(workspace / "build-report.json"), "files": len(manifest["deployment_files"]), "smoke": proof}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dotnet", type=Path, default=Path(os.environ.get("HWAOI_TOOLS", "F:") + "/DotNet/dotnet.exe"))
    parser.add_argument("--out", type=Path, default=ROOT / "work/build/hwaoi196608/xlate-managed")
    parser.add_argument("--online", action="store_true", help="allow missing package restore from nuget.org")
    parser.add_argument("--defined-float-minmax", action="store_true")
    parser.add_argument("--spirv-val", type=Path, help="optional existing spirv-val.exe for the synthetic output")
    args = parser.parse_args()
    try:
        print(json.dumps(build(args), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"xlate: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
