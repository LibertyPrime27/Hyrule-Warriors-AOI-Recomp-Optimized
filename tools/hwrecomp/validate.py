"""Differentially test selected real NSO functions against Unicorn ARM64.

Builds an original, bounded C runtime plus locally generated code as an x64
Windows DLL. No game startup, assets, OS hooks or network functionality. Input
module hashes must match the profile before code is compiled or executed.
"""
from __future__ import annotations
import os
import argparse
import ctypes as ct
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
from .a64 import translate
from .cli import emit_report
from .nso import Nso
from .nup import require


class Context(ct.Structure):
    _fields_ = [("x", ct.c_uint64 * 31), ("sp", ct.c_uint64), ("pc", ct.c_uint64),
                ("nzcv", ct.c_uint32), ("memory", ct.POINTER(ct.c_uint8)),
                ("memory_size", ct.c_size_t), ("guest_base", ct.c_uint64),
                ("budget", ct.c_uint64), ("status", ct.c_int), ("fault_address", ct.c_uint64)]


def run(exefs: Path, profile: Path, output: Path, clang: Path, cases: int, unicorn_dir=None):
    if unicorn_dir:
        sys.path.insert(0, str(Path(unicorn_dir).resolve(strict=True)))
    import unicorn as uc
    from unicorn import arm64_const as arm
    require(sys.platform == "win32" and ct.sizeof(ct.c_void_p) == 8, "validator currently requires Windows x64 Python")
    require(1 <= cases <= 10000, "cases must be in 1..10000")
    definition = json.loads(profile.read_text(encoding="utf-8"))
    modules = {}
    for name, digest in definition["modules"].items():
        require(Path(name).name == name and name not in (".", ".."), "invalid profile module name")
        module = Nso(exefs / name)
        require(module.sha256 == digest, f"profile hash mismatch: {name}; refusing to apply fixed addresses")
        modules[name] = module
    output = output.resolve()
    require(not output.exists(), "validator output exists; choose a fresh directory")
    output.mkdir(parents=True)
    source_files, payloads = [], []
    prototypes, dispatch = [], []
    for index, function in enumerate(definition["validation_functions"]):
        code = modules[function["module"]].code(function["address"], function["size"])
        symbol = f"hwr_probe_{index}"
        source = output / f"{symbol}.c"
        source.write_text(translate(code, function["address"], symbol), encoding="utf-8")
        source_files.append(str(source))
        payloads.append(code)
        prototypes.append(f"int {symbol}(HwrContext *);")
        dispatch.append(f"case {index}: return {symbol}(c);")
    wrapper = output / "dispatch.c"
    wrapper.write_text('#include "hwr_runtime.h"\n' + "\n".join(prototypes)
        + '\n__declspec(dllexport) size_t hwr_context_size(void) { return sizeof(HwrContext); }\n'
        + '__declspec(dllexport) int hwr_probe(int n, HwrContext *c) { switch(n) {\n'
        + "\n".join(dispatch) + '\ndefault: return -1; } }\n', encoding="utf-8")
    dll = output / "hwr_validation.dll"
    runtime = Path(__file__).resolve().parents[2] / "tests/prototype/include"
    command = [str(clang.resolve(strict=True)), "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror",
               "-shared", "-I", str(runtime), *source_files, str(wrapper), "-o", str(dll)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    require(result.returncode == 0, f"native compilation failed: {result.stderr}")
    native = ct.CDLL(str(dll))
    native.hwr_context_size.restype = ct.c_size_t
    require(native.hwr_context_size() == ct.sizeof(Context), "C/Python context ABI size mismatch")
    native.hwr_probe.argtypes = [ct.c_int, ct.POINTER(Context)]
    native.hwr_probe.restype = ct.c_int
    registers = [getattr(arm, f"UC_ARM64_REG_X{i}") for i in range(31)]
    rng = random.Random(0x485752)
    scratch_base, scratch_size, stop = 0x10000000, 4096, 0x20000000
    results = []
    for index, (function, code) in enumerate(zip(definition["validation_functions"], payloads)):
        start = function["address"]
        for case in range(cases):
            memory = rng.randbytes(scratch_size)
            initial = [rng.getrandbits(64) for _ in range(31)]
            initial[0], initial[30] = scratch_base + 128, stop
            nzcv = rng.randrange(16) << 28
            sp = scratch_base + scratch_size - 256
            backing = (ct.c_uint8 * scratch_size).from_buffer_copy(memory)
            c = Context()
            c.x[:] = initial
            c.sp, c.pc, c.nzcv = sp, start, nzcv
            c.memory, c.memory_size, c.guest_base, c.budget = backing, scratch_size, scratch_base, 256
            status = native.hwr_probe(index, ct.byref(c))
            require(status == 2 and c.pc == stop, f"native probe did not return: {function['label']} case {case}")
            machine = uc.Uc(uc.UC_ARCH_ARM64, uc.UC_MODE_ARM)
            page = start & ~4095
            code_mapping = ((start - page + len(code) + 4095) // 4096) * 4096
            machine.mem_map(page, code_mapping, uc.UC_PROT_READ | uc.UC_PROT_EXEC)
            machine.mem_write(start, code)
            machine.mem_map(scratch_base, scratch_size)
            machine.mem_write(scratch_base, memory)
            for register, value in zip(registers, initial):
                machine.reg_write(register, value)
            machine.reg_write(arm.UC_ARM64_REG_SP, sp)
            machine.reg_write(arm.UC_ARM64_REG_NZCV, nzcv)
            machine.emu_start(start, stop, timeout=100000, count=256)
            expected_regs = [machine.reg_read(r) for r in registers]
            expected_sp = machine.reg_read(arm.UC_ARM64_REG_SP)
            expected_flags = machine.reg_read(arm.UC_ARM64_REG_NZCV)
            expected_pc = machine.reg_read(arm.UC_ARM64_REG_PC)
            require(list(c.x) == expected_regs and c.sp == expected_sp and c.nzcv == expected_flags
                    and c.pc == expected_pc and bytes(backing) == bytes(machine.mem_read(scratch_base, scratch_size)),
                    f"Unicorn/native state mismatch: {function['label']} case {case}")
        results.append({**function, "code_sha256": hashlib.sha256(code).hexdigest(), "cases_passed": cases})
    compiler_version = subprocess.run([str(clang), "--version"], capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
    return {"result": "PASS", "functions": results, "total_cases": cases * len(results),
            "unicorn_version": uc.__version__, "compiler": compiler_version,
            "native_dll": str(dll), "dll_sha256": hashlib.sha256(dll.read_bytes()).hexdigest(),
            "comparison": "all 31 X registers, SP, PC, NZCV and all 4096 scratch-memory bytes",
            "seed": "0x485752", "module_hashes": definition["modules"],
            "limitations": ["Selected small functions only; not full instruction or gameplay validation",
                            "No real console comparison; Unicorn is an independent software oracle",
                            "No relocation, OS, GPU, audio, threading or game boot tested"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exefs", type=Path, required=True)
    parser.add_argument("--profile", type=Path,
                        default=Path(__file__).resolve().parents[2] / "config/games/hwaoi-update-196608.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--clang", type=Path, default=Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe"))
    parser.add_argument("--unicorn-dir")
    parser.add_argument("--cases", type=int, default=64)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    try:
        emit_report(run(args.exefs, args.profile, args.out, args.clang, args.cases, args.unicorn_dir), args.report)
    except (ValueError, OSError, ImportError, subprocess.TimeoutExpired) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
