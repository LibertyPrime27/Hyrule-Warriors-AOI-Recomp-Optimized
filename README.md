# HWAoI Recomp

[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)
[![Content guard](https://github.com/Alano6678/hwaoi-recomp/actions/workflows/content-guard.yml/badge.svg?branch=main)](https://github.com/Alano6678/hwaoi-recomp/actions/workflows/content-guard.yml)
[![Stars](https://img.shields.io/github/stars/Alano6678/hwaoi-recomp?style=flat)](https://github.com/Alano6678/hwaoi-recomp/stargazers)

**HWAoI Recomp** is an experimental static recompilation of *Hyrule Warriors: Age of Imprisonment* for Nintendo Switch 2 into a native Windows x86-64 application with a Vulkan renderer.

The game's ARM64 code is translated to C ahead of time and compiled with a C/C++ runtime. An NVN 2 implementation handles graphics, translating Ampere shaders to SPIR-V for Vulkan. This project builds on [DKB Recomp](https://github.com/Lucas-blip-png/dkb-pc-recomp).

> [!NOTE]
> Bring your own matching game files. No ROMs, keys, firmware, generated game code, shader payloads or game-derived executables are distributed here. Recompilation happens locally.

## Current status

**Early development build — updated November 9, 2026.** The game boots, plays both opening movies and reaches outdoor exploration and initial combat. Movement, camera controls and the opening 3D rendering work. A complete playthrough has not been verified.

Native **1080p / 60 FPS** is the current testing baseline. On the RTX 4060 / Core i5-14600KF test PC, ordinary outdoor gameplay has reached approximately **60 FPS**, with initial combat reported around **45 FPS**. These are observations from the opening areas, not a whole-game performance guarantee.

> [!WARNING]
> **Frame-rate modes other than 60 FPS still have gameplay-speed errors.** Current source corrects the native scheduler clock, but a normal-input movement comparison still shows a separate speed difference. Character, animation and combat timing remain under adaptation.
>
> **Higher-resolution modes are unstable.** Their 3D render sizes have been verified, but broader gameplay, UI and movie scaling are still being adapted.

| | Part | Progress |
| --- | --- | --- |
| ✅ | Static recompiler | All three guest modules generate, compile and link into a native Windows executable. |
| ✅ | Boot and resources | The matching build passes title/module checks and loads its game resources. Automatic GPU mirroring now checks Windows system-commit headroom. |
| ✅ | Opening movies and audio | Both opening movies play through; audio and voices work in the tested opening. |
| ✅ | Movement and camera | Keyboard/mouse movement and camera controls work; SDL controller support is integrated. |
| 🔧 | Rendering | Outdoor scenes and initial combat render. Small CPU-authored RGBA8 comparison textures up to 8×8 now have a tested conversion path; the reported crash scenes await retesting. |
| 🔧 | Combat performance | NVN2 indirect QUADS now have a GPU argument/index conversion path, avoiding their ordered CPU readback. Local GPU/recorder tests pass; crowded-scene FPS gains still need measurement. |
| 🧪 | 30 / 60 / 90 / 120 FPS | The native unlock exceeds 60 FPS. Inherited/fixed scheduler steps now follow each node's elapsed clock and preserve parent time scales. Live clock checks pass at all four targets; movement still has a separate frame-dependent speed error. |
| 🧪 | Native resolution presets | 720p, 1080p, 1440p and 4K 3D dimensions are confirmed in GPU captures. Higher resolutions remain experimental and unstable. |
| 🔧 | Playthrough coverage | Opening exploration and initial combat only; later missions remain unverified. |

✅ working in the tested scope · 🔧 adaptation in progress · 🧪 experimental

## Trying the development build

The [Releases page](https://github.com/Alano6678/hwaoi-recomp/releases) provides an experimental Windows x64 compiler package. Extract the complete ZIP, open **HWAoI-Compiler.exe**, select your own matching **NUP** and an output folder, then click **Compile / update**. Python, the C/C++ toolchain and the shader translator are bundled; no separate tool installation is needed.

After compilation, use **Open compiled game**. Keep the NUP and output folder at their original locations. Allow at least **20 GiB of free output space**; an AVX2 CPU, Vulkan GPU and sufficient memory are required (32 GiB RAM recommended). The compiler accepts only the exact build below; encrypted XCI/NSP files and other versions are unsupported. The resulting local game files are not for redistribution.

Compiler v0.1.0-alpha.5 also accepts a complete game stored as a `standalone_update` without a `base` record. It verifies both the exact executable modules and the complete resource inventory before extraction. Partial update dumps still need the matching base. NUPs that also contain merged DLC/addon packages are accepted: the verified base + update is compiled as before, and each DLC is installed under `work/game/aoc/<index>` for the game's add-on content mounts (see [DLC support](docs/hwaoi/dlc.md)). DLC dumped separately can be added with `tools/hwaoi_aoc.py from-dir`. The v0.1.0-alpha.1 compiler can report `no base package` for this layout.

Current source builds support updating or resuming an existing compiler-owned output folder. Verified resource caches, unchanged generated C and native objects are reused; saves and settings are retained, and replaced source files are backed up under `work/backups`. **Build jobs = 0** chooses generation and compilation concurrency from available RAM and system commit, with large C files limited to one concurrent compile. Compilation optimization levels are unchanged.

Source builds remain available. The inherited upstream installers and release scripts are not HWAoI-ready; this fork uses its own compiler frontend.

Only the exact build recorded in [the HWAoI game profile](config/games/hwaoi-update-196608.json) is accepted. The tools check the title and module hashes before selecting game-specific patches, and leave the original game package unchanged.

Start with the fork's entry point:

```text
python tools/hwaoi.py --help
python tools/hwaoi.py verify
```

Resource preparation, C generation, compilation/linking and launching are separate steps. The `prepare-romfs`, `generate`, `build` and `launch` subcommands describe their required inputs.

The experimental native frame-rate profile is generated with:

```text
python tools/hwaoi.py generate --rate-unlock --out work/generated/hwaoi196608-full --limit 0 --jobs 2
```

After building that generation, `HWAOI_RATE_UNLOCK=1` enables the candidate. It remains off by default while gameplay timing is being tested. The frame-rate target can then be selected in the host settings. Older generations without the matching step profile must be regenerated. `HWAOI_RATE_DIAG=1` optionally logs scheduler-step totals against wall time without altering the clock.

Press **Esc** for host settings, **F10** to cycle the performance overlay, and **F11** for fullscreen. Resolution changes require a restart. Actual FPS depends on the scene, hardware and selected resolution.

## Testing and reports

Validation includes module/hash refusal checks, independent instruction/semantic tests and Vulkan pixel-readback probes. Generation and a successful build alone do not establish playthrough coverage.

For a useful [issue report](https://github.com/Alano6678/hwaoi-recomp/issues), include the commit, GPU/CPU, resolution, frame-rate mode, where the problem occurs and the runtime log. Please keep game files and keys out of reports.

## Credits and license

Based on [DKB Recomp](https://github.com/Lucas-blip-png/dkb-pc-recomp), with its revision pinned in [config/upstream.json](config/upstream.json). Menu adaptations also credit [Savage Wind Recomp](https://github.com/Revivedx/SavageWindRecomp); the full inherited attributions remain in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

GPL-3.0-or-later overall, with inherited MIT and other third-party terms preserved. See [LICENSE](LICENSE) and [LICENSE-MIT](LICENSE-MIT).
