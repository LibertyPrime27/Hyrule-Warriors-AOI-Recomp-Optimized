# Hyrule Warriors AOI Recomp Optimized

[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)
[![Content guard](https://github.com/LibertyPrime27/Hyrule-Warriors-AOI-Recomp-Optimized/actions/workflows/content-guard.yml/badge.svg)](https://github.com/LibertyPrime27/Hyrule-Warriors-AOI-Recomp-Optimized/actions/workflows/content-guard.yml)

**Hyrule Warriors AOI Recomp Optimized** is a static recompilation of *Hyrule Warriors: Age of Imprisonment* (Nintendo Switch 2) into a native Windows x86-64 game with a Vulkan renderer. It continues earlier recomp work and adds:

- large combat performance gains
- seamless loading
- correct lighting in caves and other torch- or crystal-lit areas
- lower memory use
- two-player co-op, with each player on their own controller or keyboard
- DLC and update support
- a game icon taken from your own copy

The game's ARM64 code is translated to C ahead of time and compiled together with a C/C++ runtime. An NVN 2 implementation handles graphics and translates the game's Ampere shaders to SPIR-V for Vulkan.

## Project lead — [Alano6678](https://github.com/Alano6678)

HWAoI adaptation, runtime and renderer development, Windows compiler distribution, memory optimization, testing, and project maintenance.

## Major contributor — [LibertyPrime27](https://github.com/LibertyPrime27)

Combat performance and QUADS batching, buffer-texture lighting, first-load stutter reduction, update and DLC support, two-player controls, and compiler/settings improvements. Their work is included through [#9](https://github.com/Alano6678/hwaoi-recomp/pull/9), [#10](https://github.com/Alano6678/hwaoi-recomp/pull/10), and [#11](https://github.com/Alano6678/hwaoi-recomp/pull/11).

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

### Lighting fix: dark caves and local lights

Caves and other areas lit only by torches, crystals and other local lights rendered almost black. The game keeps its light list, light parameters and per-tile light indices in **buffer textures**. Neither the runtime nor the shader translator handled them, so every light lookup read zero:

- **The runtime** couldn't create an image for a buffer texture, so it answered the slot as 2D and bound a blank placeholder. Buffer textures are now bound as Vulkan texel buffers over the game's own memory, so light lists written by the GPU are read too.
- **The light shaders** read these buffers with a 2D zero-Y texel fetch, which the hardware treats as element X of the buffer. Translator patch `0004` now recognises that form, and graphics shaders now get texture-type answers from the host at all (combined texture handles map to slots like the runtime does).

In the cave scene that was tested, the blank texture bindings went from **292 to 0**, and local lights now match the original game.

### Loading and stutter

- **Shader pipelines build in the background**, from a pipeline key cache recorded on earlier runs. Draws whose pipeline is not ready yet are skipped instead of blocking the frame. This removes the emulator-style shader stutter and the long black stalls.
- **No more long freeze the first time a scene loads.** With an empty shader cache, entering a new scene used to freeze the game for several seconds while shaders were translated one by one. Measured on the test PC, the worst frame went from **7.5 s to about 0.9 s**, a short hitch:
  - Compute shaders, some of which take 1–2 seconds each to translate, start translating as soon as the game registers them at boot, so they are ready before the scene needs them.
  - Shaders a frame is waiting for get their own translator and skip ahead of background work.
  - The number of translator processes follows your CPU (2 to 6) and runs at below-normal priority, so it does not take CPU time from the game.
- **No freeze the first time a move plays.** A new move's effects use compute shaders the game has not run before, and their pipelines used to be built at that moment, freezing the game for **9 to 13 seconds** (measured). Their pipelines are now built in the background as soon as the shaders translate during loading. In a session with empty caches, 128 were built ahead, no move had to wait, and the worst hitch was 0.8 s.
- **Game files are verified once, not on every launch.** RomFS hash verification is saved in `romfs-verified.bin`, so later launches skip re-hashing unchanged game data. Scene loads are much shorter.

### Memory use: built for 16 GB PCs

The game commits about 16 GB at boot: its own 9.2 GB heap, plus the GPU memory Windows charges to the process. This release lowers what the runtime itself holds and keeps that commit from failing on PCs with little page file.

From the upstream HWAoI Recomp memory update by Ksora25 (November 11, 2026):

- **Compact command records.** The same command lists take about 31 % less memory (186 → 128 MiB in the opening field).
- **Compressed shader-program source.** Program source the game has not used yet is kept compressed: about 166 MiB saved.
- **Smaller allocation pools and background freeing.** Released command copies are freed on a background thread, so the game thread does not stall on it.

Added in this fork:

- **Idle command copies are compressed.** The runtime keeps a copy of every command list the game records until the game reuses its memory, which takes minutes. Copies no one has read for 2 seconds are compressed and decompressed on the rare reuse. Measured after four minutes of play: **793 MiB of copies held in 104 MiB**.
- **Unused shader code is dropped.** The SPIR-V of a shader not used for a minute is released and read back from the shader cache if it is needed again: **115 MiB** freed in play.
- **No spare blocks in system RAM.** Vulkan keeps a spare block ready only for video memory, not for system RAM.
- **Automatic low-memory heap.** At startup the game checks how much memory Windows can still promise. If the game would not fit (for example 16 GB of RAM with a small or disabled page file), it commits its heap only as it is used, which measured **1.4 to 2 GB less**. This mode stutters more while loading, so PCs with enough room do not use it. The log says which mode was chosen; `TOTK_LAZY_HEAP=0` or `1` forces either.

### Two-player co-op and controls

The game's two-player mode works. Each player is a separate Switch controller fed by their own device, so one player's input never reaches the other. Set it up in **Esc → Controls → Players and console**:

- **Player 1 / Player 2 input.**
  - **Options:** Auto, Keyboard and mouse, or any connected gamepad by name. Any brand works (Xbox, PlayStation, Switch Pro, generic), and the two players can use different brands.
  - **Ownership:** each device drives one player only. Auto gives Player 1 the first gamepad plus the keyboard and mouse, and Player 2 the next gamepad.
  - **Hot-plugging:** gamepads plugged in or out while playing are picked up.
- **Player 1 / Player 2 controller.**
  - **Options:** Pro Controller or Joy-Con pair, as the game sees it.
  - **Rumble and motion:** each follows that player's own gamepad.
- **Console mode.** Docked or Handheld, switchable live. The game is told the console was docked or undocked, as on real hardware.

When the game starts two-player mode, it asks for two controllers and receives both players set up here. The remapping rows have a **Bindings for: Player 1 / Player 2** row, so each player has their own keyboard and gamepad bindings.

The key that opens the menu is a binding too: **Open menu** is Esc by default and has no controller button until you give it one. Held for half a second, a controller's Open menu button presses Minus (the map). Any binding can be cleared with **Leave unbound** in the "Press a key" window.

Screen settings were cleaned up too:

- **Fullscreen** is a Windowed / Fullscreen setting that applies live, as well as through F11.
- **Window size** can be changed from the menu again.
- **Windowed mode** starts at 1280×720.

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

**Current version: 1.0.6 (November 11, 2026).** Version 1.0.6 lowers memory use for 16 GB PCs and removes the long freeze the first time a move plays. Version 1.0.5 made the menu key rebindable (Esc by default, any controller button you choose) and adds Leave unbound for clearing a binding. Version 1.0.4 removed the multi-second freeze when a scene loads for the first time (now a short hitch of about 0.9 s on the test PC). Version 1.0.3 added two-player co-op with per-player input devices, controllers and bindings, plus a live docked/handheld switch. The game boots, plays the opening movies and reaches outdoor exploration and combat. A complete playthrough has not been verified.

## Current priorities

1. **FPS++ timing:** keep walking, running, animation, gameplay timers and save behavior consistent at 30, 60, 90 and 120 FPS. A higher frame cap alone does not complete this work.
2. **Resolution and UI:** preserve the interface's relative size and layout at 720p, 1080p, 1440p and 4K while rendering the scene at the selected resolution.
3. **Sustained performance:** reduce crowded-combat cost and first-use stalls without dropping required draws or changing game logic.

## Feature status

| | Part | Progress |
| --- | --- | --- |
| ✅ | Static recompiler | All three guest modules generate, compile and link into a native Windows executable. |
| ✅ | Boot and resources | Title/module checks pass, and resources load from a verified cache. Verification is persisted between launches. |
| ✅ | Opening movies and audio | Both opening movies play through; audio and voices work. |
| ✅ | Movement and camera | Keyboard/mouse and SDL controller input work. |
| ✅ | Two-player co-op | Two controllers from separate devices (any gamepad brand, or keyboard and mouse), with per-player bindings. Split-screen play in every mission is not yet verified. |
| ✅ | Update 1.0.3 and DLC | Merged or separate update/DLC NUPs compile; add-on content mounts in game. |
| ✅ | Memory use | Upstream's smaller command records and bounded upload/timestamp caches are included (opening scene: 137 MiB lower private-memory peak). The vertex-cache experiment stays off by default. |
| ✅ | Combat performance | About 57 FPS average in crowded fights on the test PC (previously 13–18). |
| ✅ | Lighting | Local lights (torches, crystals, caves) work: buffer textures are bound and fetched correctly. |
| 🔧 | Rendering | Outdoor scenes, caves and combat render. Some reported crash scenes await retesting. |
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

For Windows source-build testing, `HWAOI_LAZY_HEAP=1` enables the demand-commit heap experiment. It changes when host backing is allocated, while keeping the game's logical heap size unchanged; additional pages are committed when needed. It is not yet a validated 16 GiB system preset.

Lossless program-source compression is enabled in current HWAoI source builds. `HWAOI_SOURCE_COMPRESS=0` disables it for comparison or troubleshooting; a restart is required.

Asynchronous replay can be tested with `HWAOI_REPLAY_ASYNC=1`. A same-executable pair in the stationary opening field, with 1080p and a 120 FPS target, measured median rates of **75.5 FPS synchronously and 86.3 FPS asynchronously** on the test PC with power-saving enabled. It remains disabled by default pending broader scene and synchronization checks; the gameplay-speed caveat above still applies.

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
