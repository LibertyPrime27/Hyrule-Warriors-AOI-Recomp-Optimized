# HWAoI Recomp

This is a local adaptation of DKB Recomp for Hyrule Warriors: Age of Imprisonment.
The upstream source is pinned in config/upstream.json; original licenses and
attributions must remain intact. Upstream documentation under docs/upstream is
historical reference, not the instructions for this fork.

## User environment requirement

Any downloaded compiler, SDK or other tool added to the system environment must
be installed in the tools folder named by `HWAOI_TOOLS` (default `F:\`), in a
folder named for that tool, such as `<tools>\DotNet`. Scripts and tests read
llvm-mingw, DotNet, NuGet and shaderc from there. Reuse existing tools before
installing anything. Do not install in C:\msys64.

## Content and execution boundaries

- Never commit ROMs, keys, extracted files, generated game C, shader payloads or
  game-derived binaries. Keep them under ignored work/, build/ or generated/.
- Never modify the original user-supplied NUP. Validate module hashes before
  applying a game profile. NCM version is opaque, not the display version.
- Run python tools/check_content.py before a handoff and --staged before a commit.
- Do not reuse Bananza/TotK address patches, save paths or shader-specific hacks
  for HWAoI without evidence. An unsupported path must fail visibly.
- Python is allowed for generators and build tools. Game execution uses the
  compiled C/C++ runtime. Use tools/hwaoi.py as the fork's entry point.
- Generation, object compilation, linking, boot and playability are distinct
  milestones. Report only the ones actually verified.

## Code

- Generated game code is never edited by hand; change recomp/ instead.
- Preserve third-party copyright and license notices when adapting code.
- Test profile/hash checks and failure paths. Lifter changes also need independent
  instruction/semantic tests; successful C generation is not correctness proof.
- Do not automatically upload artifacts, create a remote repository or run release
  workflows. Existing upstream installer/release tools are not HWAoI-ready.
