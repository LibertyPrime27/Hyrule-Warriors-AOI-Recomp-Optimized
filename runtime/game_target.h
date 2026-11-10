// HWAoI fork: game identity is independent of the inherited TotK version switch.
#pragma once
#define RT_TARGET_HWAOI 1
#define RT_TARGET_BANANZA 0
#define RT_GAME_TITLE "Hyrule Warriors AOI Recomp Optimized"
#define RT_GAME_TITLE_W L"Hyrule Warriors AOI Recomp Optimized"
#define RT_GAME_APPLICATION_ID 0x04003f40232de000ull
#define RT_GAME_INI "hwaoi.ini"
// Enable only after full module linking, services, RomFS and graphics validation.
#ifndef HWAOI_ALLOW_EXPERIMENTAL_BOOT
#define HWAOI_ALLOW_EXPERIMENTAL_BOOT 0
#endif
