# DLC (add-on content) support

## What the game uses

The 196608 `main` imports four add-on functions, all served on the host by
`runtime/aoc.c`:

| Import | Host behaviour |
|---|---|
| `nn::aoc::CountAddOnContent()` | number of installed add-on folders |
| `nn::aoc::ListAddOnContent(int*, int offset, int count)` | their indices in ascending order |
| `nn::fs::QueryMountAddOnContentCacheSize(size_t*, int index)` | 0 (no cache), `TargetNotFound` if not installed |
| `nn::fs::MountAddOnContent(name, index, cache, size)` | read-only mount of that add-on's files |

The SDK `aoc:u` client is never entered (the runtime has no `aoc:u` service).
Files on an add-on mount support `OpenFile` (read only), `ReadFile`,
`GetFileSize`, `CloseFile`, `GetEntryType`, `OpenDirectory`, `ReadDirectory`,
`CloseDirectory` and `Unmount`. Writes fail like they do on `rom:`.

## Installed layout

```
<output>/work/game/aoc/<index>/manifest.tsv
<output>/work/game/aoc/<index>/romfs/<files>
<output>/work/game/aoc/<index>/source.json
```

`<index>` is the AddOnContentIndex: add-on title id − application id − 0x1000
(1–2047). `manifest.tsv` starts with `HWAOI-AOC<TAB>1<TAB><title id><TAB><version>`,
then one `<sha256><TAB><size><TAB><path>` row per file, sorted by path bytes.

At start the runtime checks that every listed file exists with its size. On
the first open of each file its SHA-256 is checked; a mismatch stops the game
with a clear message instead of serving altered data. Only manifest paths are
visible to the game.

The root defaults to `aoc` beside the game RomFS folder. `HWAOI_AOC_DIR`
overrides it and `HWAOI_AOC=0` disables add-ons (Count/List report zero, as
before this change).

## Installing

- **DLC merged into the NUP** (addon and addon_update packages): the compiler
  frontend installs it automatically after the base resources. An add-on and its
  newest data patch are merged by path; unchanged installs are reused.
  Packages for other titles, and data patches without their add-on, are logged
  and skipped.
- **DLC dumped separately:** extract the add-on's RomFS with your own tools,
  then run
  `python tools/hwaoi_aoc.py from-dir <extracted romfs> <output>/work/game/aoc --index <N>`.
- `python tools/hwaoi_aoc.py from-nup <nup> <output>/work/game/aoc` repeats the
  NUP install by hand.

The base game's ExeFS and RomFS checks are unchanged: DLC never alters the
verified 196608 game inventory.

## Verification status

- Python: installer selection/merging, hash checking, reuse and refusal paths
  (`tests/hwaoi/test_aoc.py`).
- Native: `tests/hwaoi/aoc_fixture.c` exercises Count/List, mounts, reads,
  directory listings and the tamper check through `runtime/aoc.c` when the test
  Clang (`F:/llvm-mingw`) is installed.
- Not yet verified in game: no DLC dump has been available. The reference NUP
  ("1.0.3 + DLC") contains only the standalone 196608 update, with no addon
  packages.
