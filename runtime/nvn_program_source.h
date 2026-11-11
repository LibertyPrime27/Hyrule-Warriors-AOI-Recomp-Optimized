// SPDX-License-Identifier: MIT
// Immutable NVN2 source ownership. A packed program retains readable controls
// and a 128-byte code prefix for stage selection. Consumers of the complete
// source must use nvn_program_source(), including diagnostics and replacement.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include "nvn_obj.h"

typedef struct {
    uint64_t blocks, original_bytes, retained_bytes, auxiliary_bytes, index_bytes;
} NvnProgramSourceStats;

// On success, replaces/frees the original owned allocation and returns the
// replacement allocation's size. Optional allocation/compression failures leave
// the program intact and return zero. Only single-stage owned NVN2 is packed.
size_t nvn_program_source_pack(prog_t *program, size_t original_bytes);
// Expands once, checks the frame checksum, and returns a stable source pointer
// until the owning program is released. Unregistered sources pass through.
const uint8_t *nvn_program_source(const uint8_t *source);
// Call after replay has drained and before freeing program->owned_control.
// Releases auxiliary buffers; the caller still owns the fixed control/prefix.
void nvn_program_source_release(const prog_t *program);
void nvn_program_source_stats(NvnProgramSourceStats *out);
