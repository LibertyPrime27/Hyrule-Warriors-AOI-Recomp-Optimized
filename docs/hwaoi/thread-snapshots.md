# Local Windows thread snapshots

`tools/hwaoi_threads.py` captures only an explicitly selected running PID whose
executable resolves below this repository's ignored `work/build/`. It does not
launch the game or attach a debugger. The report records the executable file's
SHA-256, process creation time, helper identity, modules, host register values,
thread CPU cycle counts and native stack frames. `--exe` and `--sha256` add
explicit expected-identity checks before any thread is paused.

Example (use the current PID and build identity from the probe's running.json):

```powershell
python tools/hwaoi_threads.py --pid 1234 --exe "work/build/hwaoi196608/Hyrule Warriors AOI Recomp Optimized.exe" --sha256 EXPECTED_SHA256 --out work/reports/hang-new/threads-01.json
```

Run it again to a fresh file to compare cycle counts and stack locations. The
process creation time must match; a PID alone is not a durable identity.

The native helper is built with the existing `F:/llvm-mingw` compiler into
`work/build/hwaoi-thread-snapshot/`. No tools are installed. It matches the
selected process's Unicode executable path again and rejects a WOW64 target.
For each thread, it suspends once, copies registers and at most 256 KiB of its
committed stack, then resumes once on every ordinary capture failure path. It
does not allocate, format output or unwind while a target thread is stopped.
An existing suspension count is preserved. Each thread is resumed before the
next is inspected, so this is not an atomic snapshot of all game threads.

DbgHelp unwinds only after resume. Its memory callback serves stack reads from
the captured copy; reads elsewhere in that stack allocation fail instead of
using a changing live stack. Image/unwind metadata may be read remotely.
LLVM addr2line resolves executable frames using the PE's preferred image base
plus the observed module-relative address. Caller return addresses are resolved
one byte earlier for call-site ownership; original PCs remain in the report.
External DLL frames retain their module offsets. Stack depth is limited to 64,
missing metadata can truncate it, and register values alone do not identify a
guest `Cpu` pointer. The tool does not modify target registers or game memory.

This is local runtime evidence, not evidence produced by REA. A stack location
does not by itself prove a deadlock. Compare repeated snapshots and progress
logs. Capturing briefly perturbs scheduling. Do not forcibly terminate the
native helper while it is capturing: an operating-system API can stall, and an
external kill cannot execute its matching ResumeThread. The wrapper therefore
does not impose a hard subprocess timeout. On normal Windows API failures the
target is resumed and errors are retained in the report.

The independent sleeping-thread fixture verifies native and wrapper identity
rejections, SHA mismatch, output boundaries/overwrite rejection, real stack
unwinding through a named worker, two matching process snapshots, unchanged
zero suspension counts, and successful worker join after capture. This does
not validate every Windows driver or unusual stack/unwind layout.
