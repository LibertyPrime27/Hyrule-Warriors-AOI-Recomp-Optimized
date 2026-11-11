// Adapted from MacNativeGames games/totk/runtime/input.c and hid_feedback.c @ b75be8cf (MIT, Copyright (c) 2026
// MacNativeGames contributors).
// nn::hid overrides: one Switch controller per player (INPUT_PLAYERS), each fed only by its own host devices
// (input_poll_player: TOTK_PLAYER<n>_DEVICE picks a gamepad of any brand, the keyboard and mouse, or nothing; nothing
// pressed with TOTK_INPUT=none). Player 1 is controller id 0, player 2 id 1. In handheld mode (TOTK_OPERATION_MODE, live)
// player 1 is the handheld controller (id 0x20, style bit 1) while the game accepts handheld; HWAoI's two-player mode
// drops handheld from its styles, and player 1 is then id 0 again, as on a console with its Joy-Cons detached. Each
// player's controller is a Pro Controller (full-key, style bit 0) or a Joy-Con pair (bit 2) after TOTK_PLAYER<n>_TYPE.
// Player 2 is connected only while it has a device. Mouse and keyboard report no input. Each controller has two
// vibration devices (left, right) played on its player's gamepad motors (TOTK_RUMBLE=0: none) and a six-axis sensor fed
// by its gamepad's gyro and accelerometer (TOTK_MOTION=0, live: reads at rest). State layouts follow libnx HID.
#include <math.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "host.h"
#include "input.h"
#include "ov.h"
#include "platform/platform.h"

typedef struct { int64_t sampling; uint64_t buttons; int32_t lx, ly, rx, ry; uint32_t attributes, pad; } NpadState;
typedef struct { int64_t sampling; int32_t x, y, dx, dy, wx, wy; uint32_t buttons, attributes; } MouseState;
typedef struct { int64_t sampling; uint32_t modifiers, attributes; uint64_t keys[4]; } KeyboardState;
typedef struct { int8_t players; uint8_t pad[3]; uint32_t selected; } ControllerSupportResult;
_Static_assert(sizeof(NpadState) == 40 && sizeof(MouseState) == 40 && sizeof(KeyboardState) == 48, "hid state ABI");
_Static_assert(sizeof(ControllerSupportResult) == 8, "controller support result ABI");

// Style bits (libnx HidNpadStyleTag) and attribute bits (HidNpadAttribute).
enum { STYLE_FULL_KEY = 1, STYLE_HANDHELD = 2, STYLE_JOY_DUAL = 4 };
enum { ATTR_CONNECTED = 1, ATTR_LEFT_CONNECTED = 4, ATTR_RIGHT_CONNECTED = 0x10 };

static PlatMutex lock = PLAT_MUTEX_INIT;
static volatile uint32_t styles = STYLE_FULL_KEY;   // SetSupportedNpadStyleSet; read without the lock
static uint32_t handheld_activation_mode, joy_hold_type;
static int enabled, mouse_ready, keyboard_ready;
// Controller ids the game named in SetSupportedNpadIdType (bit id for 0..7, bit 8 for 0x10, bit 9 for 0x20), and the
// ids it disconnected (DisconnectNpad) until the controller-support applet brings them back.
static volatile uint32_t supported_ids = 0x3ff, disconnected_ids;
static int64_t sample;

static uint32_t id_bit(uint32_t id) { return id < 8 ? 1u << id : id == 0x10 ? 1u << 8 : id == 0x20 ? 1u << 9 : 0; }

// Player p's controller id: player 1 is the handheld controller in handheld mode while the game accepts that style.
static uint32_t player_id(int p) {
    uint32_t s = __atomic_load_n(&styles, __ATOMIC_RELAXED);
    return p == 0 && ov_handheld() && (s & STYLE_HANDHELD) ? 0x20u : (uint32_t)p;
}

// The style player p's controller has: handheld for player 1 in handheld mode, else TOTK_PLAYER<n>_TYPE ("dual": a
// Joy-Con pair, when the game accepts one; anything else a Pro Controller).
static uint32_t player_style(int p) {
    if (player_id(p) == 0x20) return STYLE_HANDHELD;
    char key[32];
    snprintf(key, sizeof key, "TOTK_PLAYER%d_TYPE", p + 1);
    uint32_t s = __atomic_load_n(&styles, __ATOMIC_RELAXED);
    return !strcmp(cfg_value(key), "dual") && (s & STYLE_JOY_DUAL) ? STYLE_JOY_DUAL : STYLE_FULL_KEY;
}

static int player_of(uint32_t id) {
    for (int p = 0; p < INPUT_PLAYERS; p++)
        if (player_id(p) == id) return p;
    return -1;
}

// Player p's controller is there for the game: npads initialized, its id supported and not disconnected, and the
// player has a device.
static int player_live(int p) {
    uint32_t bit = id_bit(player_id(p));
    return enabled && (__atomic_load_n(&supported_ids, __ATOMIC_RELAXED) & bit) &&
           !(__atomic_load_n(&disconnected_ids, __ATOMIC_RELAXED) & bit) && input_player_connected(p);
}

static const uint32_t *need_id(Cpu *c, int i, const char *fn) {
    const uint32_t *id = arg_p(c, i);
    if (!id) rt_fatal("nn::hid::%s: null controller id", fn);
    return id;
}

// void InitializeNpad()
static void ov_initialize_npad(Cpu *c) {
    (void)c;
    plat_mutex_lock(&lock);
    enabled = 1;
    plat_mutex_unlock(&lock);
    rt_log("hid: controllers initialized (%d players; player 1 %s)", INPUT_PLAYERS, ov_handheld() ? "handheld" : "full-key");
}

// void SetSupportedNpadIdType(const NpadIdType *ids, size_t count)
static void ov_supported_ids(Cpu *c) {
    const uint32_t *ids = arg_p(c, 0);
    uint64_t n = arg_x(c, 1);
    if (n > 10 || (!ids && n)) rt_fatal("nn::hid::SetSupportedNpadIdType: invalid id list (%llu ids)", (unsigned long long)n);
    uint32_t mask = 0;
    char list[96] = "";
    for (uint64_t i = 0; i < n; i++) {
        mask |= id_bit(ids[i]);
        size_t l = strlen(list);
        snprintf(list + l, sizeof list - l, "%s%#x", i ? " " : "", ids[i]);
    }
    rt_log("hid: the game supports controller ids [%s]", list);
    __atomic_store_n(&supported_ids, mask, __ATOMIC_RELAXED);
}

// void SetSupportedNpadStyleSet(NpadStyleSet styles)
static void ov_supported_styles(Cpu *c) {
    // Bits: 0 full-key (Pro), 1 handheld, 2 Joy-Con pair, 3 left Joy-Con, 4 right Joy-Con (libnx HidNpadStyleTag).
    uint32_t s = (uint32_t)arg_x(c, 0);
    uint32_t was = __atomic_exchange_n(&styles, s, __ATOMIC_RELAXED);
    if (s != was) rt_log("hid: the game supports controller styles %#x", s);
}

// NpadStyleSet GetNpadStyleSet(const NpadIdType &id)
static void ov_style_set(Cpu *c) {
    uint32_t id = *need_id(c, 0, "GetNpadStyleSet");
    int p = player_of(id);
    ret_x(c, p >= 0 && player_live(p) ? __atomic_load_n(&styles, __ATOMIC_RELAXED) & player_style(p) : 0);
}

// int GetNpadStates(<State> *out, int count, const NpadIdType &id): the newest state only. want: the style of the
// overload, 0 for the single Joy-Con overloads (never connected).
static void npad_read_state(NpadState *out, uint32_t id, uint32_t want) {
    if (!out) rt_fatal("nn::hid::GetNpadStates: null output");
    static uint32_t seen[11];   // per id 0..7, 0x10, 0x20: the styles read so far (logged once each)
    int slot = id < 8 ? (int)id : id == 0x10 ? 8 : id == 0x20 ? 9 : 10;
    uint32_t bit = want ? want : 0x80000000u;
    if (!(__atomic_fetch_or(&seen[slot], bit, __ATOMIC_RELAXED) & bit))
        rt_log("hid: the game reads controller id %#x as style %#x", id, want);
    plat_mutex_lock(&lock);
    NpadState s = {.sampling = ++sample};
    plat_mutex_unlock(&lock);
    int p = player_of(id);
    if (want && p >= 0 && want == player_style(p) && (__atomic_load_n(&styles, __ATOMIC_RELAXED) & want) &&
        player_live(p) && input_poll_player(p, &s.buttons, &s.lx, &s.ly, &s.rx, &s.ry))
        s.attributes = ATTR_CONNECTED | (want == STYLE_JOY_DUAL ? ATTR_LEFT_CONNECTED | ATTR_RIGHT_CONNECTED : 0);
    else
        s.buttons = 0, s.lx = s.ly = s.rx = s.ry = 0;
    *out = s;
}
static void npad_states(Cpu *c, uint32_t want) {
    int max = (int)arg_x(c, 1);
    uint32_t id = *need_id(c, 2, "GetNpadStates");
    if (max <= 0) { ret_x(c, 0); return; }
    npad_read_state(arg_p(c, 0), id, want);
    ret_x(c, 1);
}
static void ov_states_full_key(Cpu *c) { npad_states(c, STYLE_FULL_KEY); }
static void ov_states_handheld(Cpu *c) { npad_states(c, STYLE_HANDHELD); }
static void ov_states_joy_dual(Cpu *c) { npad_states(c, STYLE_JOY_DUAL); }
static void ov_states_joycon(Cpu *c) { npad_states(c, 0); }

// HWAoI imports the singular void overloads. The SDK implements those through
// its internal shared-memory reader, not a relocatable call to the public plural
// overload; handle both ABIs explicitly with the same host controller state.
static void npad_state(Cpu *c, uint32_t want) {
    npad_read_state(arg_p(c, 0), *need_id(c, 1, "GetNpadState"), want);
}
static void ov_state_full_key(Cpu *c) { npad_state(c, STYLE_FULL_KEY); }
static void ov_state_handheld(Cpu *c) { npad_state(c, STYLE_HANDHELD); }
static void ov_state_joy_dual(Cpu *c) { npad_state(c, STYLE_JOY_DUAL); }
static void ov_state_joycon(Cpu *c) { npad_state(c, 0); }

// void SetNpadJoyHoldType(NpadJoyHoldType type)
static void ov_hold_type(Cpu *c) {
    int64_t t = (int32_t)arg_x(c, 0);
    if (t < 0 || t > 1) rt_fatal("nn::hid::SetNpadJoyHoldType: invalid hold type %lld", (long long)t);
    plat_mutex_lock(&lock);
    joy_hold_type = (uint32_t)t;
    plat_mutex_unlock(&lock);
}

static void ov_get_hold_type(Cpu *c) {
    plat_mutex_lock_shared(&lock);
    ret_x(c, joy_hold_type);
    plat_mutex_unlock_shared(&lock);
}

// libnx HID: 0/1/2 require two/one/zero attached Joy-Cons for handheld activation.
// The existing host handheld model is a complete virtual controller, so every
// supported requirement is satisfied; docked full-key input is unaffected.
static void ov_handheld_activation(Cpu *c) {
    uint32_t mode = (uint32_t)arg_x(c, 0);
    if (mode > 2) rt_fatal("hid: invalid handheld activation mode %u", mode);
    plat_mutex_lock(&lock);
    handheld_activation_mode = mode;
    plat_mutex_unlock(&lock);
    rt_log("hid: handheld attachment requirement %u stored for the host controller", mode);
}

static void ov_get_handheld_activation(Cpu *c) {
    plat_mutex_lock_shared(&lock);
    ret_x(c, handheld_activation_mode);
    plat_mutex_unlock_shared(&lock);
}

// void DisconnectNpad(const NpadIdType &id): that controller reads as disconnected until the controller-support applet
// connects players again (handheld mode disconnects player 1's full-key id).
static void ov_disconnect(Cpu *c) {
    uint32_t id = *need_id(c, 0, "DisconnectNpad");
    __atomic_fetch_or(&disconnected_ids, id_bit(id), __ATOMIC_RELAXED);
    rt_log("hid: the game disconnected controller id %#x", id);
}

// Result ShowControllerSupport(ControllerSupportResultInfo *out, const ControllerSupportArg &arg): the players set up
// in the menu are the controllers it returns, players 1..n in order (the applet's own screen is not shown). HWAoI's
// two-player mode asks for exactly two; with player 2 unassigned only one comes back and the game stays single.
static void ov_controller_support(Cpu *c) {
    ControllerSupportResult *out = arg_p(c, 0);
    const uint8_t *arg = arg_p(c, 1);   // ControllerSupportArg: player count min, max, then option flags
    int min = arg ? (int8_t)arg[0] : 1, max = arg ? (int8_t)arg[1] : 1;
    if (arg)
        rt_log("hid: controller support applet: players %d..%d, flags %02x %02x %02x %02x %02x %02x", min, max,
               arg[2], arg[3], arg[4], arg[5], arg[6], arg[7]);
    if (max < 1 || max > INPUT_PLAYERS) max = max < 1 ? 1 : INPUT_PLAYERS;
    int players = 1;
    while (players < max && input_player_connected(players)) players++;
    for (int p = 0; p < players; p++) __atomic_fetch_and(&disconnected_ids, ~id_bit(player_id(p)), __ATOMIC_RELAXED);
    if (players < min)
        rt_log("hid: the game asked for %d players but only %d have a device: assign player 2's input in the Controls "
               "menu (Esc)", min, players);
    else
        rt_log("hid: controller support applet returns %d player%s", players, players == 1 ? "" : "s");
    if (out) *out = (ControllerSupportResult){.players = (int8_t)players, .selected = player_id(0)};
    ret_x(c, 0);
}

static void ov_initialize_mouse(Cpu *c) {
    (void)c;
    plat_mutex_lock(&lock);
    mouse_ready = 1;
    plat_mutex_unlock(&lock);
}

static void ov_initialize_keyboard(Cpu *c) {
    (void)c;
    plat_mutex_lock(&lock);
    keyboard_ready = 1;
    plat_mutex_unlock(&lock);
}

// int GetMouseStates(MouseState *out, int count): a mouse that is not connected
static void ov_mouse_states(Cpu *c) {
    MouseState *out = arg_p(c, 0);
    int max = (int)arg_x(c, 1);
    if (max <= 0) { ret_x(c, 0); return; }
    if (!out) rt_fatal("nn::hid::GetMouseStates: null output");
    plat_mutex_lock(&lock);
    if (!mouse_ready) rt_fatal("nn::hid::GetMouseStates before InitializeMouse");
    *out = (MouseState){.sampling = ++sample};
    plat_mutex_unlock(&lock);
    ret_x(c, 1);
}

// void GetKeyboardState(KeyboardState *out): a keyboard with no key down
static void ov_keyboard_state(Cpu *c) {
    KeyboardState *out = arg_p(c, 0);
    if (!out) rt_fatal("nn::hid::GetKeyboardState: null output");
    plat_mutex_lock(&lock);
    if (!keyboard_ready) rt_fatal("nn::hid::GetKeyboardState before InitializeKeyboard");
    *out = (KeyboardState){.sampling = ++sample};
    plat_mutex_unlock(&lock);
}

// ---- vibration and six-axis sensor (MacNativeGames hid_feedback.c)
// Handles are the SDK's (nn::hid::utility, sdk+0x4a6e00 and sdk+0x4a6ab0): byte 0 the style index (3 full-key,
// 4 handheld), byte 1 the controller id, byte 2 the device (vibration 0 left / 1 right, six-axis 2). Every call under
// the public functions goes through nn::hid::detail (sdk's PLT), answered here instead of by the hid service, including
// the batches VibrationTarget queues through detail::EnqueueVibrationValue into detail::SendVibrationValues.
// VibrationValue {amplitude low, frequency low, amplitude high, frequency high}: 0..1 and Hz, neutral 0/160/0/320.
typedef struct { float amp_low, freq_low, amp_high, freq_high; } VibrationValue;
typedef struct { uint32_t type, position; } VibrationDeviceInfo;   // type 1 linear resonant actuator; position 1 left, 2 right
_Static_assert(sizeof(VibrationValue) == 16 && sizeof(VibrationDeviceInfo) == 8, "vibration ABI");

enum { STYLE_INDEX_FULL_KEY = 3, STYLE_INDEX_HANDHELD = 4, SIX_AXIS_DEVICE = 2, RUMBLE_MS = 250 };
#define RUMBLE_STALE_NS 250000000ull     // a side the game has not updated for 250 ms is silent
#define RUMBLE_REFRESH_NS 50000000ull    // an unchanged value is sent again every 50 ms (its 250 ms start over)
#define RUMBLE_MIN_GAP_NS 10000000ull    // a changed value at most every 10 ms (the game streams every few ms)
static const VibrationValue vib_neutral = {0, 160, 0, 320};

static int env_zero(const char *name) {
    const char *e = cfg_get(name);
    return e && !strcmp(e, "0");
}

static void once(volatile int32_t *flag, const char *fmt, uint32_t v) {
    if (!plat_xchg32(flag, 1)) rt_log(fmt, v);
}

// TOTK_RUMBLE_STRENGTH (percent, 0..300, default 130; 0 = off) and TOTK_RUMBLE_CURVE (exponent on the amplitude, 0.2..1.5,
// default 0.6). HD rumble is strong at low amplitude, an ERM motor hardly turns there, so weak values are lifted by the
// curve (0.1 -> 0.25) and the strength scales the result (saturating at 65535; 0.66 and up reach full drive at 130 %).
// Both are live settings (the menu): read again whenever cfg_version changes.
static float rumble_strength = 1.3f, rumble_curve = 0.6f;
static volatile int32_t rumble_cfg_logged;
static unsigned rumble_cfg_seen = ~0u;
static float cfg_float(const char *name, float lo, float hi) {
    double v = cfg_double(name);
    return (float)(v < lo ? lo : v > hi ? hi : v);
}
static void rumble_cfg(void) {
    unsigned v = cfg_version();
    if (v == rumble_cfg_seen) return;
    rumble_cfg_seen = v;
    rumble_strength = cfg_float("TOTK_RUMBLE_STRENGTH", 0, 300) / 100;
    rumble_curve = cfg_float("TOTK_RUMBLE_CURVE", 0.2f, 1.5f);
    int trig = input_rumble_triggers_enabled();
    if (!plat_xchg32(&rumble_cfg_logged, 1))
        rt_log("hid: rumble strength %.0f%%, curve %.2f, triggers %s", rumble_strength * 100, rumble_curve, trig ? "on" : "off");
}
static uint16_t motor(float a) { return rumble_motor(a, rumble_curve, rumble_strength); }

// A handle's style index (byte 0): 3 full-key, 4 handheld, 5 Joy-Con pair.
static uint32_t style_index(uint32_t style) {
    return style == STYLE_HANDHELD ? STYLE_INDEX_HANDHELD : style == STYLE_JOY_DUAL ? 5u : STYLE_INDEX_FULL_KEY;
}

// The player whose controller a handle names (byte 0 its style index, byte 1 its id), or -1.
static int handle_player(uint32_t v) {
    for (int p = 0; p < INPUT_PLAYERS; p++)
        if ((v & 0xff) == style_index(player_style(p)) && (v >> 8 & 0xff) == player_id(p)) return p;
    return -1;
}

// The SDK's id check (utility functions): players 1-8, other (0x10) and handheld (0x20); the public functions abort
// on another id.
static uint32_t handle_id(Cpu *c, const char *fn) {
    uint32_t id = *need_id(c, 2, fn);
    if (id >= 8 && id != 0x10 && id != 0x20) rt_fatal("nn::hid::%s: invalid controller id %#x", fn, id);
    return id;
}

// int GetVibrationDeviceHandles(VibrationDeviceHandle *out, int count, const NpadIdType &id, NpadStyleSet style):
// utility::GetVibrationDeviceHandles' table, the first style bit in its order; none with TOTK_RUMBLE=0.
static void ov_vibration_handles(Cpu *c) {
    uint32_t *out = arg_p(c, 0), style = (uint32_t)arg_x(c, 3);
    int max = (int)arg_x(c, 1);
    uint32_t id = handle_id(c, "GetVibrationDeviceHandles");
    if (!out || max <= 0 || __builtin_popcount(style) != 1)
        rt_fatal("nn::hid::GetVibrationDeviceHandles: invalid arguments (count %d, styles %#x)", max, style);
    static volatile int32_t asked;
    rumble_cfg();
    // strength 0 keeps the devices (silent), so the menu can turn rumble up again
    if (env_zero("TOTK_RUMBLE")) { once(&asked, "hid: no vibration devices (TOTK_RUMBLE=%u)", 0); ret_x(c, 0); return; }
    static const struct { uint8_t bit, n; uint32_t base; } t[] = {
        {0, 2, 0x3}, {1, 2, 0x4}, {2, 2, 0x5}, {3, 1, 0x6}, {4, 1, 0x10007}, {5, 1, 0x8}, {10, 1, 0xd}, {8, 2, 0x4},
        {30, 2, 0x21}, {29, 2, 0x20}};
    int n = 0;
    for (size_t i = 0; i < sizeof t / sizeof *t && !n; i++) {
        if (!(style >> t[i].bit & 1)) continue;
        n = t[i].n < max ? t[i].n : max;
        for (int k = 0; k < n; k++) out[k] = (t[i].base | id << 8) + ((uint32_t)k << 16);
    }
    once(&asked, "hid: the game asked for its vibration devices (first handle %#x)", n ? out[0] : 0);
    ret_x(c, (uint64_t)n);
}

// int GetSixAxisSensorHandles(SixAxisSensorHandle *out, int count, const NpadIdType &id, NpadStyleSet style):
// utility::GetSixAxisSensorHandle's order (one sensor per style, two for the Joy-Con pair). Also with TOTK_MOTION=0: the
// sensor then reads at rest (six_fill), so the menu can turn motion on while the game runs.
static void ov_six_axis_handles(Cpu *c) {
    uint32_t *out = arg_p(c, 0), style = (uint32_t)arg_x(c, 3);
    int max = (int)arg_x(c, 1);
    uint32_t id = handle_id(c, "GetSixAxisSensorHandles");
    if (!out || max <= 0) rt_fatal("nn::hid::GetSixAxisSensorHandles: invalid arguments (count %d)", max);
    static volatile int32_t asked;
    uint32_t base = 0, n = 1;
    if (style & 1) base = 0x20003;
    else if (style & 2) base = 0x20004;
    else if (style & 4) base = 0x5, n = 2;
    else if (style & 8) base = 0x6;
    else if (style & 0x10) base = 0x10007;
    else if (style & 0x20) base = 0x20003;
    else if (style & 0xe80) n = 0;
    else if (style & 0x100) base = 0x20004;
    else if ((style & 0x60000040) == 0x40) base = 0x20003;
    else n = 0;
    if (n > (uint32_t)max) n = (uint32_t)max;
    for (uint32_t k = 0; k < n; k++) out[k] = (base | id << 8) + (k << 16);
    once(&asked, "hid: the game asked for its six-axis sensor (first handle %#x)", n ? out[0] : 0);
    ret_x(c, n);
}

static const uint32_t *need_handle(const void *p, const char *fn) {
    if (!p) rt_fatal("nn::hid::detail::%s: null handle", fn);
    return p;
}

// The vibration device a handle names, as player * 2 + side (0 left, 1 right), or -1 (logged once).
static int vibration_side(const uint32_t *h, const char *fn) {
    uint32_t v = *need_handle(h, fn), dev = v >> 16 & 0xff;
    int p = handle_player(v);
    if (p >= 0 && dev < 2) return p * 2 + (int)dev;
    static volatile int32_t warned;
    once(&warned, "hid: vibration call for handle %#x, not a device of a connected controller: ignored", v);
    return -1;
}

static PlatMutex vib_lock = PLAT_MUTEX_INIT;
static VibrationValue vib_last[INPUT_PLAYERS][2] = {{{0, 160, 0, 320}, {0, 160, 0, 320}},
                                                    {{0, 160, 0, 320}, {0, 160, 0, 320}}};   // vib_lock
static uint64_t vib_at[INPUT_PLAYERS][2], sent_at[INPUT_PLAYERS];                            // vib_lock
static uint16_t sent_low[INPUT_PLAYERS], sent_high[INPUT_PLAYERS];                           // vib_lock
static int vib_allowed = 1;                                                                  // vib_lock: PermitVibration

static float clamp01(float v) { return v > 0 ? (v < 1 ? v : 1) : 0; }   // NaN -> 0
// Share of a band at f Hz that goes to the light (high-frequency, right) motor: none at 160 Hz and below (the low band's
// neutral frequency), all at 320 Hz and above (the high band's), log scale between.
static float light_share(float f) { return f > 160 ? (f < 320 ? log2f(f / 160) : 1) : 0; }

// Switch HD rumble -> SDL_RumbleGamepad. Each side's two bands split by frequency between the heavy (low) and the light
// (high) motor (light_share) and add up as energy per motor (root of the summed squares); the stronger side drives each
// motor. A side the game has not updated for 250 ms is silent. Each send lasts 250 ms and is renewed every 50 ms while
// the game streams, so the pad stops by itself when the game stops sending (pause, freeze). Under vib_lock.
static void rumble_update(int p, uint64_t now) {
    float heavy = 0, light = 0;
    rumble_cfg();
    for (int s = 0; s < 2 && vib_allowed; s++) {
        if (!vib_at[p][s] || now - vib_at[p][s] > RUMBLE_STALE_NS) continue;
        const VibrationValue *v = &vib_last[p][s];
        float al = clamp01(v->amp_low), ah = clamp01(v->amp_high), sl = light_share(v->freq_low), sh = light_share(v->freq_high);
        float h = sqrtf(al * (1 - sl) * al * (1 - sl) + ah * (1 - sh) * ah * (1 - sh));
        float l = sqrtf(al * sl * al * sl + ah * sh * ah * sh);
        if (h > heavy) heavy = h;
        if (l > light) light = l;
    }
    uint16_t lo = motor(clamp01(heavy)), hi = motor(clamp01(light));
    int changed = lo != sent_low[p] || hi != sent_high[p], on = lo || hi;
    if (changed ? on && now - sent_at[p] < RUMBLE_MIN_GAP_NS : !on || now - sent_at[p] < RUMBLE_REFRESH_NS) return;
    input_rumble_player(p, lo, hi, on ? RUMBLE_MS : 0);
    sent_low[p] = lo, sent_high[p] = hi, sent_at[p] = now;
}

static void play(int target, const VibrationValue *v) {
    if (!v) rt_fatal("nn::hid::detail::SendVibrationValue: null value");
    if (target < 0) return;
    int p = target / 2, side = target % 2;
    VibrationValue c = *v;
    if (!isfinite(c.amp_low) || !isfinite(c.amp_high) || !isfinite(c.freq_low) || !isfinite(c.freq_high)) c = vib_neutral;
    static volatile int32_t first, first_on;
    if (!plat_xchg32(&first, 1))
        rt_log("hid: first vibration value from the game: %s %.2f@%.0f Hz %.2f@%.0f Hz", side ? "right" : "left",
               (double)c.amp_low, (double)c.freq_low, (double)c.amp_high, (double)c.freq_high);
    if ((c.amp_low > 0.01f || c.amp_high > 0.01f) && !plat_xchg32(&first_on, 1))
        rt_log("hid: first non-zero vibration from the game: %s %.2f@%.0f Hz %.2f@%.0f Hz", side ? "right" : "left",
               (double)c.amp_low, (double)c.freq_low, (double)c.amp_high, (double)c.freq_high);
    uint64_t now = kern_now_ns();
    plat_mutex_lock(&vib_lock);
    vib_last[p][side] = c;
    vib_at[p][side] = now;
    rumble_update(p, now);
    plat_mutex_unlock(&vib_lock);
}

// Result detail::InitializeVibrationDevice(const VibrationDeviceHandle &)
static void ov_vib_init(Cpu *c) {
    static volatile int32_t logged;
    const uint32_t *h = arg_p(c, 0);
    if (vibration_side(h, "InitializeVibrationDevice") >= 0)
        once(&logged, "hid: the game initialized its vibration devices (handle %#x)", *h);
    ret_x(c, 0);
}

// Result detail::SendVibrationValue(const VibrationDeviceHandle &, const VibrationValue &)
static void ov_vib_send(Cpu *c) {
    play(vibration_side(arg_p(c, 0), "SendVibrationValue"), arg_p(c, 1));
    ret_x(c, 0);
}

// Result detail::SendVibrationValues(const VibrationDeviceHandle *, const VibrationValue *, int count)
static void ov_vib_send_many(Cpu *c) {
    const uint32_t *h = arg_p(c, 0);
    const VibrationValue *v = arg_p(c, 1);
    int n = (int)arg_x(c, 2);
    if (n < 0 || (n && (!h || !v))) rt_fatal("nn::hid::detail::SendVibrationValues: invalid batch (%d values)", n);
    for (int i = 0; i < n; i++) play(vibration_side(&h[i], "SendVibrationValues"), &v[i]);
    ret_x(c, 0);
}

// Result detail::GetActualVibrationValue(VibrationValue *out, const VibrationDeviceHandle &): the last value sent
static void ov_vib_actual(Cpu *c) {
    VibrationValue *out = arg_p(c, 0);
    if (!out) rt_fatal("nn::hid::detail::GetActualVibrationValue: null output");
    int target = vibration_side(arg_p(c, 1), "GetActualVibrationValue");
    VibrationValue v = vib_neutral;
    plat_mutex_lock_shared(&vib_lock);
    if (target >= 0) v = vib_last[target / 2][target % 2];
    plat_mutex_unlock_shared(&vib_lock);
    *out = v;
    ret_x(c, 0);
}

// Result detail::GetVibrationDeviceInfo(VibrationDeviceInfo *out, const VibrationDeviceHandle &)
static void ov_vib_info(Cpu *c) {
    VibrationDeviceInfo *out = arg_p(c, 0);
    if (!out) rt_fatal("nn::hid::detail::GetVibrationDeviceInfo: null output");
    int target = vibration_side(arg_p(c, 1), "GetVibrationDeviceInfo");
    *out = target >= 0 ? (VibrationDeviceInfo){1, (uint32_t)(target % 2) + 1} : (VibrationDeviceInfo){0, 0};
    ret_x(c, 0);
}

// Result detail::IsVibrationDeviceMounted(bool *out, const VibrationDeviceHandle &)
static void ov_vib_mounted(Cpu *c) {
    uint8_t *out = arg_p(c, 0);
    if (!out) rt_fatal("nn::hid::detail::IsVibrationDeviceMounted: null output");
    *out = vibration_side(arg_p(c, 1), "IsVibrationDeviceMounted") >= 0;
    ret_x(c, 0);
}

// Result detail::IsVibrationPermitted(bool *out)
static void ov_vib_permitted(Cpu *c) {
    uint8_t *out = arg_p(c, 0);
    if (!out) rt_fatal("nn::hid::detail::IsVibrationPermitted: null output");
    plat_mutex_lock_shared(&vib_lock);
    *out = (uint8_t)vib_allowed;
    plat_mutex_unlock_shared(&vib_lock);
    ret_x(c, 0);
}

// Result detail::PermitVibration(bool): off silences the gamepad at once
static void ov_vib_permit(Cpu *c) {
    int on = (arg_x(c, 0) & 0xff) != 0;
    plat_mutex_lock(&vib_lock);
    vib_allowed = on;
    for (int p = 0; p < INPUT_PLAYERS; p++) rumble_update(p, kern_now_ns());
    plat_mutex_unlock(&vib_lock);
    ret_x(c, 0);
}

static volatile int32_t six_running[INPUT_PLAYERS];   // StartSixAxisSensor .. StopSixAxisSensor, per player

// The player whose six-axis sensor a handle names (device 2; a Joy-Con pair's left or right Joy-Con), or -1.
static int six_axis_player(const uint32_t *h, const char *fn) {
    uint32_t v = *need_handle(h, fn), dev = v >> 16 & 0xff;
    int p = handle_player(v);
    if (p >= 0 && (dev == SIX_AXIS_DEVICE || (player_style(p) == STYLE_JOY_DUAL && dev < 2))) return p;
    static volatile int32_t warned;
    once(&warned, "hid: six-axis call for handle %#x, not a connected controller's sensor: reads at rest", v);
    return -1;
}

// Result detail::StartSixAxisSensor(const SixAxisSensorHandle &): angle and direction start over
static void ov_six_start(Cpu *c) {
    const uint32_t *h = arg_p(c, 0);
    int p = six_axis_player(h, "StartSixAxisSensor");
    if (p >= 0) {
        static volatile int32_t logged;
        once(&logged, "hid: the game started the six-axis sensor (handle %#x)", *h);
        motion_restart(input_motion_player(p));
        input_motion_start();
        plat_xchg32(&six_running[p], 1);
    }
    ret_x(c, 0);
}

// Result detail::StopSixAxisSensor(const SixAxisSensorHandle &)
static void ov_six_stop(Cpu *c) {
    int p = six_axis_player(arg_p(c, 0), "StopSixAxisSensor");
    if (p >= 0) plat_xchg32(&six_running[p], 0);
    ret_x(c, 0);
}

// All max entries, newest first: the game reads all 16 it asks for and ignores the count (main+0x2a268e0). A short
// history repeats its oldest sample. No motion (sensor not started, no gamepad, one without a gyro, TOTK_MOTION=0):
// a controller lying still, not marked connected.
static int six_fill(MotionState *s, int max, const uint32_t *h, const char *fn) {
    if (max <= 0) return 0;
    if (!s) rt_fatal("nn::hid::detail::%s: null output", fn);
    int p = six_axis_player(h, fn);
    int live = p >= 0 && six_running[p] && input_motion_enabled();
    int n = live ? motion_read(input_motion_player(p), s, max, kern_now_ns()) : 0;
    if (!n) {
        static const MotionState rest = {.acc = {0, 0, -1}, .dir = {1, 0, 0, 0, 1, 0, 0, 0, 1}};
        for (int i = 0; i < max; i++) s[i] = rest;
    } else
        for (int i = n; i < max; i++) s[i] = s[n - 1];
    static volatile int32_t first_rest, first_live;
    if (!n && p >= 0 && six_running[p]) once(&first_rest, "hid: six-axis read with no motion samples: at rest (%u states)", (uint32_t)max);
    if (n) once(&first_live, "hid: first six-axis read with live motion (%u samples)", (uint32_t)n);
    return max;
}

// Result detail::GetSixAxisSensorStates(int *count, SixAxisSensorState *out, int max, const SixAxisSensorHandle &)
static void ov_six_states(Cpu *c) {
    int32_t *count = arg_p(c, 0);
    if (!count) rt_fatal("nn::hid::detail::GetSixAxisSensorStates: null count");
    *count = six_fill(arg_p(c, 1), (int)arg_x(c, 2), arg_p(c, 3), "GetSixAxisSensorStates");
    ret_x(c, 0);
}

// Result detail::GetSixAxisSensorState(SixAxisSensorState *out, const SixAxisSensorHandle &)
static void ov_six_state(Cpu *c) {
    six_fill(arg_p(c, 0), 1, arg_p(c, 1), "GetSixAxisSensorState");
    ret_x(c, 0);
}

// ---- Switch 2 SDK (Donkey Kong Bananza): public names for what TotK's SDK had under nn::hid::detail, and the
// touch screen and Joy-Con assignment calls TotK never made.

// int GetSixAxisSensorStates(SixAxisSensorState *out, int count, const SixAxisSensorHandle &): the count is returned
static void ov_six_states_public(Cpu *c) {
    ret_x(c, (uint64_t)(int64_t)six_fill(arg_p(c, 0), (int)arg_x(c, 1), arg_p(c, 2), "GetSixAxisSensorStates"));
}

// void InitializeTouchScreen(): there is no touch screen on the host
static void ov_touch_init(Cpu *c) {
    static volatile int32_t logged;
    (void)c;
    once(&logged, "hid: the game initialized the touch screen (none on this host)", 0);
}

// void GetTouchScreenState<1>(TouchScreenState<1> *out): sampling number, then a count of 0 touches
static void ov_touch_state1(Cpu *c) {
    static volatile int64_t sampling;
    uint8_t *out = arg_p(c, 0);
    if (!out) rt_fatal("nn::hid::GetTouchScreenState: null output");
    memset(out, 0, 0x38);   // int64 sampling number, int32 count, int32 reserved, one 0x28-byte TouchState
    int64_t n = __atomic_add_fetch(&sampling, 1, __ATOMIC_RELAXED);
    memcpy(out, &n, 8);
}

// void SetNpadJoyAssignmentModeDual(const NpadIdType &), SetNpadJoyAssignmentModeSingle(const NpadIdType &,
// NpadJoyDeviceType), Result MergeSingleJoyAsDualJoy(const NpadIdType &, const NpadIdType &): the host's one pad is
// always a full controller, so the assignment mode changes nothing
static void ov_joy_assignment(Cpu *c) { ret_x(c, 0); }

HOST_OVERRIDE("_ZN2nn3hid18StartSixAxisSensorERKNS0_19SixAxisSensorHandleE", ov_six_start);
HOST_OVERRIDE("_ZN2nn3hid22GetSixAxisSensorStatesEPNS0_18SixAxisSensorStateEiRKNS0_19SixAxisSensorHandleE",
              ov_six_states_public);
HOST_OVERRIDE("_ZN2nn3hid18SendVibrationValueERKNS0_21VibrationDeviceHandleERKNS0_14VibrationValueE", ov_vib_send);
HOST_OVERRIDE("_ZN2nn3hid25InitializeVibrationDeviceERKNS0_21VibrationDeviceHandleE", ov_vib_init);
HOST_OVERRIDE("_ZN2nn3hid22GetVibrationDeviceInfoEPNS0_19VibrationDeviceInfoERKNS0_21VibrationDeviceHandleE", ov_vib_info);
HOST_OVERRIDE("_ZN2nn3hid21InitializeTouchScreenEv", ov_touch_init);
HOST_OVERRIDE("_ZN2nn3hid19GetTouchScreenStateILm1EEEvPNS0_16TouchScreenStateIXT_EEE", ov_touch_state1);
HOST_OVERRIDE("_ZN2nn3hid28SetNpadJoyAssignmentModeDualERKj", ov_joy_assignment);
HOST_OVERRIDE("_ZN2nn3hid30SetNpadJoyAssignmentModeSingleERKjNS0_17NpadJoyDeviceTypeE", ov_joy_assignment);
HOST_OVERRIDE("_ZN2nn3hid23MergeSingleJoyAsDualJoyERKjS2_", ov_joy_assignment);

HOST_OVERRIDE("_ZN2nn3hid14InitializeNpadEv", ov_initialize_npad);
HOST_OVERRIDE("_ZN2nn3hid22SetSupportedNpadIdTypeEPKjm", ov_supported_ids);
HOST_OVERRIDE("_ZN2nn3hid24SetSupportedNpadStyleSetENS_4util10BitFlagSetILi32ENS0_12NpadStyleTagEEE", ov_supported_styles);
HOST_OVERRIDE("_ZN2nn3hid15GetNpadStyleSetERKj", ov_style_set);
HOST_OVERRIDE("_ZN2nn3hid13GetNpadStatesEPNS0_16NpadFullKeyStateEiRKj", ov_states_full_key);
HOST_OVERRIDE("_ZN2nn3hid13GetNpadStatesEPNS0_16NpadJoyDualStateEiRKj", ov_states_joy_dual);
HOST_OVERRIDE("_ZN2nn3hid13GetNpadStatesEPNS0_16NpadJoyLeftStateEiRKj", ov_states_joycon);
HOST_OVERRIDE("_ZN2nn3hid13GetNpadStatesEPNS0_17NpadJoyRightStateEiRKj", ov_states_joycon);
HOST_OVERRIDE("_ZN2nn3hid13GetNpadStatesEPNS0_17NpadHandheldStateEiRKj", ov_states_handheld);
HOST_OVERRIDE("_ZN2nn3hid12GetNpadStateEPNS0_16NpadFullKeyStateERKj", ov_state_full_key);
HOST_OVERRIDE("_ZN2nn3hid12GetNpadStateEPNS0_16NpadJoyDualStateERKj", ov_state_joy_dual);
HOST_OVERRIDE("_ZN2nn3hid12GetNpadStateEPNS0_16NpadJoyLeftStateERKj", ov_state_joycon);
HOST_OVERRIDE("_ZN2nn3hid12GetNpadStateEPNS0_17NpadJoyRightStateERKj", ov_state_joycon);
HOST_OVERRIDE("_ZN2nn3hid12GetNpadStateEPNS0_17NpadHandheldStateERKj", ov_state_handheld);
HOST_OVERRIDE("_ZN2nn3hid18SetNpadJoyHoldTypeENS0_15NpadJoyHoldTypeE", ov_hold_type);
HOST_OVERRIDE("_ZN2nn3hid18GetNpadJoyHoldTypeEv", ov_get_hold_type);
HOST_OVERRIDE("_ZN2nn3hid29SetNpadHandheldActivationModeENS0_26NpadHandheldActivationModeE", ov_handheld_activation);
HOST_OVERRIDE("_ZN2nn3hid29GetNpadHandheldActivationModeEv", ov_get_handheld_activation);
HOST_OVERRIDE("_ZN2nn3hid14DisconnectNpadERKj", ov_disconnect);
HOST_OVERRIDE("_ZN2nn3hid21ShowControllerSupportEPNS0_27ControllerSupportResultInfoERKNS0_20ControllerSupportArgE",
              ov_controller_support);
HOST_OVERRIDE("_ZN2nn3hid15InitializeMouseEv", ov_initialize_mouse);
HOST_OVERRIDE("_ZN2nn3hid18InitializeKeyboardEv", ov_initialize_keyboard);
HOST_OVERRIDE("_ZN2nn3hid14GetMouseStatesEPNS0_10MouseStateEi", ov_mouse_states);
HOST_OVERRIDE("_ZN2nn3hid16GetKeyboardStateEPNS0_13KeyboardStateE", ov_keyboard_state);
HOST_OVERRIDE("_ZN2nn3hid25GetVibrationDeviceHandlesEPNS0_21VibrationDeviceHandleEiRKjNS_4util10BitFlagSetILi32ENS0_"
              "12NpadStyleTagEEE", ov_vibration_handles);
HOST_OVERRIDE("_ZN2nn3hid23GetSixAxisSensorHandlesEPNS0_19SixAxisSensorHandleEiRKjNS_4util10BitFlagSetILi32ENS0_"
              "12NpadStyleTagEEE", ov_six_axis_handles);
HOST_OVERRIDE("_ZN2nn3hid6detail25InitializeVibrationDeviceERKNS0_21VibrationDeviceHandleE", ov_vib_init);
HOST_OVERRIDE("_ZN2nn3hid6detail18SendVibrationValueERKNS0_21VibrationDeviceHandleERKNS0_14VibrationValueE", ov_vib_send);
HOST_OVERRIDE("_ZN2nn3hid6detail19SendVibrationValuesEPKNS0_21VibrationDeviceHandleEPKNS0_14VibrationValueEi",
              ov_vib_send_many);
HOST_OVERRIDE("_ZN2nn3hid6detail23GetActualVibrationValueEPNS0_14VibrationValueERKNS0_21VibrationDeviceHandleE",
              ov_vib_actual);
HOST_OVERRIDE("_ZN2nn3hid6detail22GetVibrationDeviceInfoEPNS0_19VibrationDeviceInfoERKNS0_21VibrationDeviceHandleE",
              ov_vib_info);
HOST_OVERRIDE("_ZN2nn3hid6detail24IsVibrationDeviceMountedEPbRKNS0_21VibrationDeviceHandleE", ov_vib_mounted);
HOST_OVERRIDE("_ZN2nn3hid6detail20IsVibrationPermittedEPb", ov_vib_permitted);
HOST_OVERRIDE("_ZN2nn3hid6detail15PermitVibrationEb", ov_vib_permit);
HOST_OVERRIDE("_ZN2nn3hid6detail18StartSixAxisSensorERKNS0_19SixAxisSensorHandleE", ov_six_start);
HOST_OVERRIDE("_ZN2nn3hid6detail17StopSixAxisSensorERKNS0_19SixAxisSensorHandleE", ov_six_stop);
HOST_OVERRIDE("_ZN2nn3hid6detail22GetSixAxisSensorStatesEPiPNS0_18SixAxisSensorStateEiRKNS0_19SixAxisSensorHandleE",
              ov_six_states);
HOST_OVERRIDE("_ZN2nn3hid6detail21GetSixAxisSensorStateEPNS0_18SixAxisSensorStateERKNS0_19SixAxisSensorHandleE",
              ov_six_state);
