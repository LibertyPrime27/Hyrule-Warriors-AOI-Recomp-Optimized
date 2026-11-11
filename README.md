# Hyrule Warriors AOI Recomp Optimized

[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)
[![Content guard](https://github.com/LibertyPrime27/Hyrule-Warriors-AOI-Recomp-Optimized/actions/workflows/content-guard.yml/badge.svg)](https://github.com/LibertyPrime27/Hyrule-Warriors-AOI-Recomp-Optimized/actions/workflows/content-guard.yml)

**Hyrule Warriors AOI Recomp Optimized** is a static recompilation of *Hyrule Warriors: Age of Imprisonment* (Nintendo Switch 2) into a native Windows x86-64 game with a Vulkan renderer. It continues earlier recomp work and adds:

- large combat performance gains
- seamless loading
- DLC and update support
- a game icon taken from your own copy

The game's ARM64 code is translated to C ahead of time and compiled together with a C/C++ runtime. An NVN 2 implementation handles graphics and translates the game's Ampere shaders to SPIR-V for Vulkan.

> [!NOTE]
> Bring your own matching game files. No ROMs, keys, firmware, generated game code, shader payloads, game artwork or game-derived executables are distributed here. Recompilation happens locally on your PC.

## Credits

This project stands on the work of others:

- **[HWAoI Recomp](https://github.com/Alano6678/hwaoi-recomp) by Alano6678.** The original Hyrule Warriors: Age of Imprisonment recompilation: the game profile, recompiler adaptation, NVN 2 work, compiler front end and everything that first got the game booting and playable. This repository is a fork of it, and includes its November 11, 2026 memory update by Ksora25.
- **[DKB Recomp](https://github.com/Lucas-blip-png/dkb-pc-recomp) by Lucas-blip-png.** The *Donkey Kong Bananza* static recompilation that HWAoI Recomp is built on. It provides the recompiler, runtime, Vulkan/NVN layer and shader translator. The imported revision is pinned in [config/upstream.json](config/upstream.json).
- **[Savage Wind Recomp](https://github.com/Revivedx/SavageWindRecomp).** Credited for menu adaptations.

The full inherited attributions are kept in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## What this fork adds

### Combat performance: about 3–4× faster fights

Before this fork, crowded combat dropped to **13–18 FPS** on an RTX 3080 / i9-10850K at 1080p. The same fights now average about **57 FPS**. Three changes made the difference:

- **A wrong stage-number check sent every quad draw down a stalling path.** The NVN 2 indirect QUADS conversion compared shader stages in the wrong numbering, so every quad draw fell back to a CPU readback path. That path waits for the GPU to finish. The check now uses NVN's own stage order, so quads stay on the GPU.
- **Quad conversions are batched.** Consecutive quad draws share one compute dispatch pass and a single pair of barriers instead of paying for them per draw. The quad index buffer is now a static pattern filled once, so the extra expand shader is gone.
- **Guest barriers are recorded only when needed.** A guest `nvnCommandBufferBarrier` used to become a full Vulkan pipeline drain. Now only masks that actually order earlier work (the ORDER bits) record a barrier. Invalidate-only barriers are dropped, which removes 80–88 % of combat drains. `TOTK_BARRIER_ELIDE=0` restores the old behaviour.

Combat is now CPU-bound rather than stalled on the GPU.

### Loading and stutter

- **Shader pipelines build in the background**, from a pipeline key cache recorded on earlier runs. Draws whose pipeline is not ready yet are skipped instead of blocking the frame. This removes the emulator-style shader stutter and the long black stalls.
- **Game files are verified once, not on every launch.** RomFS hash verification is saved in `romfs-verified.bin`, so later launches skip re-hashing unchanged game data. Scene loads are much shorter.

### Memory use (from upstream)

This fork includes the upstream HWAoI Recomp memory update (November 11, 2026):

- **Smaller command records.** Viewport and fixed-state command records are packed to the size they use.
- **Bounded caches.** At most eight completed upload batches are kept, timestamp slots are reused, and the query pool is released at shutdown.
- **Measured upstream:** in the 1080p / 60 FPS opening scene, the retained command-copy peak dropped from **1128 to 1013 MiB** and the process private-memory peak fell by **137 MiB**, with no FPS change.
- **Optional vertex cache.** An opt-in read-only vertex snapshot cache (`TOTK_VERTEX_CACHE`) is included but **off by default**, because its low-memory path is still too slow for normal play.

### DLC and update support

- **Merged NUPs work.** A NUP with the game, the 1.0.3 update and DLC merged together compiles directly. DLC packages are installed under `work/game/aoc/<index>`.
- **The compiler window has two optional fields.** *Update file* takes a separate update NUP, which is verified together with the base game. *DLC file* takes a separate DLC NUP. Leave both blank when your game file already includes them.
- **DLC is served to the game natively.** The runtime implements the game's add-on content calls (count, list and mount) with read-only, hash-checked mounts. See [DLC support](docs/hwaoi/dlc.md).
- **The 1.0.3 standalone update is verified** as a complete game, even without a base record.

### Frame rate: 60 FPS plus frame generation

The 30 / 60 / 90 / 120 FPS settings are available, both in the compiler (**Allow 90 / 120 FPS settings**, on by default) and in the in-game **Esc** menu.

> [!WARNING]
> **90 and 120 FPS speed up the whole game (1.5× and 2×).** The game's logic advances one fixed step per frame. Live tests on build 196608 showed:
> - walking speed ignores the scheduler clock and its time-block scale
> - the vsync signal is already a fixed 60 Hz
>
> Rendering more frames therefore runs the game faster.

**For smooth motion at normal speed, play at 60 FPS and use frame generation**, for example [Lossless Scaling](https://store.steampowered.com/app/993090/Lossless_Scaling/) LSFG 2× for 120 FPS output. Fullscreen (F11) is borderless rather than exclusive, so capture-based frame generation works. Turn it on once the game window appears.

### Name and icon

- **The compiled game and the compiler are named "Hyrule Warriors AOI Recomp Optimized"**, including the in-game version line.
- **The game's icon is the cover art from your own NUP.** It is read from the package metadata while compiling and embedded in the executable and game window, so no artwork ships in this repository.

## Current status

**Current version: 1.0.1 (November 9, 2026).** Version 1.0.1 fixes compiling, which failed at the final link step in 1.0.0. The game boots, plays the opening movies and reaches outdoor exploration and combat. A complete playthrough has not been verified.

| | Part | Progress |
| --- | --- | --- |
| ✅ | Static recompiler | All three guest modules generate, compile and link into a native Windows executable. |
| ✅ | Boot and resources | Title/module checks pass, and resources load from a verified cache. Verification is persisted between launches. |
| ✅ | Opening movies and audio | Both opening movies play through; audio and voices work. |
| ✅ | Movement and camera | Keyboard/mouse and SDL controller input work. |
| ✅ | Update 1.0.3 and DLC | Merged or separate update/DLC NUPs compile; add-on content mounts in game. |
| 🔧 | Memory use | Upstream's smaller command records and bounded upload/timestamp caches are included. The vertex-cache experiment stays off by default. |
| ✅ | Combat performance | About 57 FPS average in crowded fights on the test PC (previously 13–18). |
| 🔧 | Rendering | Outdoor scenes and combat render. Some reported crash scenes await retesting. |
| 🧪 | 90 / 120 FPS | Selectable, but they speed up gameplay. Use 60 FPS plus frame generation. |
| 🧪 | Native resolution presets | 720p, 1080p, 1440p and 4K render sizes are confirmed. Higher resolutions are unstable. |
| 🔧 | Playthrough coverage | Opening exploration and early combat only; later missions remain unverified. |

✅ working in the tested scope · 🔧 adaptation in progress · 🧪 experimental

## Using it

1. **Get the compiler.** Download the ZIP from the [latest release](https://github.com/LibertyPrime27/Hyrule-Warriors-AOI-Recomp-Optimized/releases/latest) and extract the whole ZIP to a writable folder. Python, the C/C++ toolchain and the shader translator are bundled, so nothing else needs installing.
2. **Open Hyrule Warriors AOI Recomp Optimized Compiler.exe.** Select your own matching game **NUP** (and optionally an update or DLC NUP), choose an output folder, then click **Compile / update**.
3. **Click Open compiled game** when the build finishes.

Requirements and limits:

- **Output space:** at least **20 GiB free**.
- **Hardware:** an AVX2 CPU, a Vulkan GPU and enough memory (32 GiB RAM recommended).
- **Accepted game files:** only the exact build below. Encrypted XCI/NSP files and other versions are unsupported.
- **After compiling:** keep the game NUP where it is. Update and DLC files are copied into the output.
- **Updating an existing output folder:** verified game data, generated C and native objects are reused. Saves and settings are kept, and replaced source files are backed up under `work/backups`.
- **Sharing:** the resulting local game files are not for redistribution.

Only the exact build recorded in [the HWAoI game profile](config/games/hwaoi-update-196608.json) is accepted (title `04003F40232DE000`, NCM 196608 = version 1.0.3). The tools check the title and module hashes before selecting game-specific patches, and never modify the original game package.

From source, start with the fork's entry point:

```text
python tools/hwaoi.py --help
python tools/hwaoi.py verify
```

Resource preparation, C generation, compilation/linking and launching are separate steps (`prepare-romfs`, `generate`, `build`, `launch`). The frame-rate profile is generated with `generate --rate-unlock` and enabled at launch by `HWAOI_RATE_UNLOCK=1`. `HWAOI_RATE_DIAG=1` logs scheduler-step totals against wall time.

In game, press **Esc** for settings, **F10** to cycle the performance overlay and **F11** for fullscreen. Resolution changes need a restart.

## Testing and reports

Validation includes:

- module/hash refusal checks
- instruction and semantic tests
- Vulkan pixel-readback probes
- native tests for DLC mounts, quad batching and barrier elision

A successful generation and build alone do not prove playthrough coverage.

When reporting a problem, include:

- the commit
- your GPU and CPU
- the resolution and frame-rate setting
- where the problem happens
- the runtime log

Keep game files and keys out of reports.

## License

GPL-3.0-or-later overall, with inherited MIT and other third-party terms preserved. See [LICENSE](LICENSE), [LICENSE-MIT](LICENSE-MIT) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
