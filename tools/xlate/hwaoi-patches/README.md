# HWAoI-only translator patches

These patches are applied to the managed builder's private source copy after the
unchanged upstream patch selection. `tools/vendor/ryujinx` is never edited.
`tools/build_xlate_hwaoi.py` includes each extra patch's name/content digest in
the translator identity and refuses a skipped application.

## 0001: private register banks, protocol version 1

The Ampere lowerer may emit the explicit private MOV format used by Eglantine's
`NativeRegisterCopy` (reference commit
`30604547544e6f2c766262850b88a31a2404c538`). It carries source/destination bank
numbers 0..2 in otherwise reserved bits and a strict marker. This is an internal
lowerer-to-translator format, not an invented NVIDIA GPU opcode. The adapter and
mask/SSA changes here are original; underlying source licenses remain intact.

Only a translation job containing integer `"hwaoi_ir_banks": 1` enables it.
Absent metadata, a different version, malformed marker/predicate/quad fields or
bank 3 are rejected. Ordinary Maxwell MOV behavior remains unchanged.

Architectural RZ stays index **255**. IR GPR capacity is **768**, so bank 1's
register 255 is real IR register **511**, and bank 2's is **767**. Predicate/flag
keys move after all 768 IR GPRs; register-use dataflow uses twelve 64-bit GPR masks
plus separate predicate and flag masks. The mask storage is a value type, not a
shared mutable array.

The library's original synthetic probe checks mapping, feature refusal, RZ,
live-mask boundaries and three independent GPR/predicate/flag SSA phis. The
builder additionally sends a hand-assembled private-bank shader through the
actual translation pipeline. In the initial validated build its SPIR-V is
byte-identical to the original constant-color shader after optimization.
This is not a real-GPU semantic test or proof that all native shaders are correct.

The older `xlate-managed` deployment is preserved. Protocol-capable deployment:
`work/build/hwaoi196608/xlate-managed-irbanks` (see its build-report.json and .id).

## 0003: proven typed storage-buffer address bases

For the explicit guarded NVN 2 compute contract, a complete low/high address
certificate can select a declared storage-buffer table entry. Each low-word
addition must have its matching unsigned carry in the high word; shifted or
signed upper words must also match. Unresolved phis, unrelated carries and
unknown arithmetic retain the original guarded candidates. This prevents scalar
dimensions and floating-point parameters from becoming broad writable aliases
when their pointer provenance can be ruled out. NVN descriptor bounds remain
enforced by the generated guards.

The managed builder includes the original CPU-only address probe in its source
identity and requires `--probe-hwaoi-typed-storage-address` before deployment.
It checks carry/refusal cases, predicate phis with copies, and independent
64-bit arithmetic oracles. This source addition still requires compilation and
real combat validation; it is not evidence of a measured FPS improvement.

## 0004: explicit-level texel fetches of buffer textures

HWAoI's deferred-light shaders read their buffer textures (light lists, light
parameters, cluster index tables) with `TLD 1D .LL`: an integer-coordinate 1D
texel fetch with an explicit level of detail, which the hardware ignores for a
buffer. Upstream asks the host for the sampler type only without a level, so
these fetches were emitted as 1D-as-2D reads of a texture that does not exist
and every local light contributed zero; caves lit only by torches and crystals
rendered almost black. With this patch an explicit-level, non-bindless 1D
integer fetch asks the host and becomes a texel-buffer fetch (level dropped)
when the bound descriptor is a buffer texture. Bindless explicit-level fetches
keep the 1D type: nothing distinguishes them from a mipmapped 1D texture.
