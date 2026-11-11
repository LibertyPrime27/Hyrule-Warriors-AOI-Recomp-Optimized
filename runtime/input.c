// Host input for nn::hid (runtime/ov_input.c): an SDL3 gamepad (no SDL window needed), the keyboard and the mouse,
// merged into Switch npad button bits and stick axes. Button order follows libnx HidNpadButton (ISC). The key and pad
// bindings are the tables in input_map.c (totk.ini overrides them). The keyboard and the mouse come from the window
// system's back end (input_wsys.h: input_win.c with Win32, input_sdl.c with SDL), which also owns the cursor capture.
// Keys and their map: docs/controls.md.
// The gamepad also rumbles (input_rumble) and, when it has a gyro and an accelerometer, feeds the six-axis sensor from a
// thread of its own (input_motion_start). Every SDL call on the gamepad is made under `lock`.
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include "input_feed.h"
#include <string.h>
#include <SDL3/SDL.h>

#include "cfg.h"
#include "host.h"
#include "input.h"
#include "input_map.h"
#include "input_wsys.h"
#include "nvn_api.h"
#include "ui/menu.h"
#include "vk.h"
#include "platform/platform.h"

enum { STICK_MAX = 32767, STICK_DIR = 16384, TRIGGER_ON = 16384, RESCAN_MS = 1000 };

static PlatMutex lock = PLAT_MUTEX_INIT;
static int state;                    // 0 not started, 1 running, -1 disabled or SDL failed
static uint64_t next_scan;

// Every connected gamepad is open, in connection order. Each belongs to at most one player (assign below): co-op runs
// player 1 and player 2 from separate devices, so one never moves the other. All fields under `lock`.
enum { MAX_PADS = 8 };
typedef struct {
    SDL_Gamepad *g;
    SDL_JoystickID id;
    char name[64], guid[33];
    int rumble;                      // has rumble motors
    int east_a;                      // labels its east face button A (Nintendo layout under TOTK_PAD_LAYOUT=auto)
    int layout;                      // PAD_LAYOUT_* in effect, -1 to pick again; live (cfg_version)
    unsigned layout_seen;
    int32_t labels[4];               // labels of its south, east, west, north buttons (PAD_LABEL_*)
    int sensor_set, sensor_ok;       // the motion thread set its sensors up; it has both, enabled
} Pad;
static Pad pads[MAX_PADS];
static int npads;
static unsigned pads_version;        // bumped whenever a gamepad opens or closes

static volatile int32_t pad_labels[4];           // player 1's gamepad's face labels, for the menu (any thread)
static volatile int32_t pad_layout_shown = -1;   // player 1's gamepad's layout for the menu (any thread), -1 without one
static int motion_thread;            // 0 not started, 1 running, -1 off (TOTK_MOTION=0, no SDL)

// SDL buttons of the gamepad inputs (input_map.h PAD_*), by position; PAD_ZL and PAD_ZR are the triggers (axes).
static const SDL_GamepadButton pad_sdl[PAD_COUNT] = {
    [PAD_SOUTH] = SDL_GAMEPAD_BUTTON_SOUTH, [PAD_EAST] = SDL_GAMEPAD_BUTTON_EAST, [PAD_WEST] = SDL_GAMEPAD_BUTTON_WEST,
    [PAD_NORTH] = SDL_GAMEPAD_BUTTON_NORTH, [PAD_LSTICK] = SDL_GAMEPAD_BUTTON_LEFT_STICK,
    [PAD_RSTICK] = SDL_GAMEPAD_BUTTON_RIGHT_STICK, [PAD_L] = SDL_GAMEPAD_BUTTON_LEFT_SHOULDER,
    [PAD_R] = SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER, [PAD_START] = SDL_GAMEPAD_BUTTON_START, [PAD_BACK] = SDL_GAMEPAD_BUTTON_BACK,
    [PAD_DPAD_LEFT] = SDL_GAMEPAD_BUTTON_DPAD_LEFT, [PAD_DPAD_UP] = SDL_GAMEPAD_BUTTON_DPAD_UP,
    [PAD_DPAD_RIGHT] = SDL_GAMEPAD_BUTTON_DPAD_RIGHT, [PAD_DPAD_DOWN] = SDL_GAMEPAD_BUTTON_DPAD_DOWN,
    [PAD_LPADDLE1] = SDL_GAMEPAD_BUTTON_LEFT_PADDLE1, [PAD_RPADDLE1] = SDL_GAMEPAD_BUTTON_RIGHT_PADDLE1,
    [PAD_LPADDLE2] = SDL_GAMEPAD_BUTTON_LEFT_PADDLE2, [PAD_RPADDLE2] = SDL_GAMEPAD_BUTTON_RIGHT_PADDLE2,
};

// TOTK_INPUT_SCRIPT=<file>: lines "<frame> <button> [hold]" or "<frame> LSTICK|RSTICK <x> <y> [hold]", ORed into the real input.
// <frame> and its hold are 60 Hz ticks of game time since the first present (frame_step_ticks: presents at 60 fps), so
// the title menus get the presses at the same game time at any frame limit. A line "@<ms> ..." is timed instead: <ms>
// and its hold are milliseconds of the world clock (frame_step_world_us, game time since the loaded save's first
// frame), so it lands at the same game time at any frame rate and load length. A timed event shorter than a frame is
// applied once. In a script with timed lines (a recording) the untimed ones belong before the world clock: they stop
// when it starts, as the load ends at another tick in each run.
// at, hold: ticks, or microseconds of the world clock when timed.
typedef struct { uint64_t at, hold; int bit, stick, timed; int32_t x, y; int logged; char text[48]; } ScriptEv;
static const struct { const char *name; int bit; } script_buttons[] = {
    {"A", B_A}, {"B", B_B}, {"X", B_X}, {"Y", B_Y}, {"LCLICK", B_STICK_L}, {"RCLICK", B_STICK_R}, {"L", B_L}, {"R", B_R},
    {"ZL", B_ZL}, {"ZR", B_ZR}, {"PLUS", B_PLUS}, {"MINUS", B_MINUS}, {"LEFT", B_LEFT}, {"UP", B_UP}, {"RIGHT", B_RIGHT}, {"DOWN", B_DOWN},
};
static ScriptEv *script_ev;
static int script_n, script_state;   // 0 not loaded, 1 loaded
static int script_mixed;             // the script has timed lines: its untimed ones end where the world clock starts

static void script_load(void) {
    script_state = 1;
    const char *path = cfg_get("TOTK_INPUT_SCRIPT");
    if (!path || !*path) return;
    FILE *f = fopen(path, "r");
    if (!f) { rt_log("input: cannot open TOTK_INPUT_SCRIPT %s", path); return; }
    char line[256];
    int cap = 0, ln = 0;
    while (fgets(line, sizeof line, f)) {
        ln++;
        char *h = strchr(line, '#');
        if (h) *h = 0;
        char name[32];
        double at;
        int off = 0, timed = line[0] == '@';
        if (sscanf(line + timed, "%lf %31s%n", &at, name, &off) < 2 || at < 0) continue;
        off += timed;
        double unit = timed ? 1000.0 : 1.0;   // ms to us, or presents
        ScriptEv e = {.at = (uint64_t)(at * unit + 0.5), .hold = timed ? 66667 : 4, .bit = -1, .timed = timed};
        long a, b;
        double c;
        if (!strcmp(name, "LSTICK") || !strcmp(name, "RSTICK")) {
            int n = sscanf(line + off, "%ld %ld %lf", &a, &b, &c);
            if (n < 2) { rt_log("input: script line %d: %s needs x y [hold]", ln, name); continue; }
            e.stick = name[0] == 'L' ? 1 : 2; e.x = (int32_t)a; e.y = (int32_t)b;
            if (n >= 3 && c > 0) e.hold = (uint64_t)(c * unit + 0.5);
            snprintf(e.text, sizeof e.text, "%s %d %d", name, e.x, e.y);
        } else {
            for (size_t i = 0; i < sizeof script_buttons / sizeof *script_buttons; i++)
                if (!strcmp(name, script_buttons[i].name)) e.bit = script_buttons[i].bit;
            if (e.bit < 0) { rt_log("input: script line %d: unknown button %s", ln, name); continue; }
            if (sscanf(line + off, "%lf", &c) == 1 && c > 0) e.hold = (uint64_t)(c * unit + 0.5);
            snprintf(e.text, sizeof e.text, "%s", name);
        }
        if (script_n == cap) {
            cap = cap ? cap * 2 : 32;
            script_ev = realloc(script_ev, (size_t)cap * sizeof *script_ev);
            if (!script_ev) { script_n = 0; fclose(f); return; }
        }
        script_ev[script_n++] = e;
        script_mixed |= timed;
    }
    fclose(f);
    rt_log("input: script %s, %d events", path, script_n);
}

static void script_apply(uint64_t *b, int32_t sl[2], int32_t sr[2]) {
    if (!script_state) script_load();
    if (!script_n) return;
    uint64_t fr = __atomic_load_n(&gfx_counters.frames, __ATOMIC_RELAXED), tick = frame_step_ticks();
    int64_t w = frame_step_world_us();
    for (int i = 0; i < script_n; i++) {
        ScriptEv *e = &script_ev[i];
        if (e->timed ? w < 0 : script_mixed && w >= 0) continue;
        uint64_t now = e->timed ? (uint64_t)w : tick;
        if (now < e->at || (now >= e->at + e->hold && (e->logged || !e->timed))) continue;
        if (!e->logged) {
            e->logged = 1;
            if (e->timed) rt_log("input: script frame %llu at %.1f ms: %s", (unsigned long long)fr, w / 1000.0, e->text);
            else rt_log("input: script frame %llu tick %llu: %s", (unsigned long long)fr, (unsigned long long)tick, e->text);
        }
        if (e->stick) { int32_t *s = e->stick == 1 ? sl : sr; s[0] = e->x; s[1] = e->y; }
        else *b |= 1ull << e->bit;
    }
}

// TOTK_INPUT_RECORD=<file>: writes the merged input as TOTK_INPUT_SCRIPT lines, one per button press and per stick
// segment, so a played session replays as a script. A stick segment ends when an axis moves by more than REC_STICK_STEP.
// An event that begins on the world clock is written timed ("@<ms>", hold in ms), one before it in ticks
// (frame_step_ticks). When the world clock starts, held buttons and sticks are cut there and go on as timed events.
enum { REC_STICK_STEP = 2048 };
static FILE *rec;
static int rec_state;                // 0 not opened, 1 recording, -1 off
static uint64_t rec_down[16];        // tick + 1 at which each button went down, 0 while up
static int64_t rec_down_w[16];       // the world clock then, -1 before it started
static uint64_t rec_stick_from[2];   // tick + 1 at which the current non-zero stick segment began
static int64_t rec_stick_w[2];
static int32_t rec_stick[2][2];
static int64_t rec_last_w = -1;      // the world clock at the previous apply

// "<tick> <what> <hold>" or "@<ms> <what> <hold ms>": from tick from and world clock from_w to now.
static void rec_line(uint64_t from, int64_t from_w, uint64_t fr, int64_t w, const char *what) {
    if (from_w >= 0 && w >= from_w)
        fprintf(rec, "@%.1f %s %.1f\n", from_w / 1000.0, what, (w > from_w ? w - from_w : 1000) / 1000.0);
    else
        fprintf(rec, "%llu %s %llu\n", (unsigned long long)from, what, (unsigned long long)(fr > from ? fr - from : 1));
}

static void rec_close_stick(int s, uint64_t fr, int64_t w) {
    if (!rec_stick_from[s]) return;
    char what[48];
    snprintf(what, sizeof what, "%s %d %d", s ? "RSTICK" : "LSTICK", rec_stick[s][0], rec_stick[s][1]);
    rec_line(rec_stick_from[s] - 1, rec_stick_w[s], fr, w, what);
    rec_stick_from[s] = 0;
}

static void rec_flush_all(void) {
    if (rec_state != 1) return;
    uint64_t fr = frame_step_ticks() + 1;
    int64_t w = frame_step_world_us();
    for (int i = 0; i < 16; i++)
        if (rec_down[i]) { rec_line(rec_down[i] - 1, rec_down_w[i], fr, w, script_buttons[i].name); rec_down[i] = 0; }
    rec_close_stick(0, fr, w);
    rec_close_stick(1, fr, w);
    fflush(rec);
}

static void rec_apply(uint64_t b, const int32_t sl[2], const int32_t sr[2]) {
    if (!rec_state) {
        const char *path = cfg_get("TOTK_INPUT_RECORD");   // no default: not recording when unset
        rec = path && *path ? fopen(path, "w") : NULL;
        rec_state = rec ? 1 : -1;
        if (path && *path && !rec) rt_log("input: cannot open TOTK_INPUT_RECORD %s", path);
        if (rec) { fprintf(rec, "# Recorded input (TOTK_INPUT_RECORD); frame = 60 Hz ticks of game time, @ms = world clock.\n"); atexit(rec_flush_all);
                   rt_log("input: recording to %s", path); }
    }
    if (rec_state != 1) return;
    uint64_t fr = frame_step_ticks();
    int64_t w = frame_step_world_us();
    int wrote = 0;
    if (w >= 0 && rec_last_w < 0) {   // the world clock starts: what is held goes on as timed events from @0
        for (int i = 0; i < 16; i++)
            if (rec_down[i]) {
                rec_line(rec_down[i] - 1, -1, fr, -1, script_buttons[i].name);
                rec_down[i] = fr + 1, rec_down_w[i] = w;
                wrote = 1;
            }
        for (int s = 0; s < 2; s++)
            if (rec_stick_from[s]) {
                rec_close_stick(s, fr, -1);
                rec_stick_from[s] = fr + 1, rec_stick_w[s] = w;
                wrote = 1;
            }
    }
    rec_last_w = w;
    for (int i = 0; i < 16; i++) {
        int bit = script_buttons[i].bit, down = (b >> bit & 1) != 0;
        if (down && !rec_down[i]) rec_down[i] = fr + 1, rec_down_w[i] = w;
        else if (!down && rec_down[i]) {
            rec_line(rec_down[i] - 1, rec_down_w[i], fr, w, script_buttons[i].name);
            rec_down[i] = 0;
            wrote = 1;
        }
    }
    const int32_t *st[2] = {sl, sr};
    for (int s = 0; s < 2; s++) {
        int moved = abs(st[s][0] - rec_stick[s][0]) > REC_STICK_STEP || abs(st[s][1] - rec_stick[s][1]) > REC_STICK_STEP ||
                    ((st[s][0] || st[s][1]) != (rec_stick[s][0] || rec_stick[s][1]));
        if (!moved) continue;
        if (rec_stick_from[s]) { rec_close_stick(s, fr, w); wrote = 1; }
        rec_stick[s][0] = st[s][0]; rec_stick[s][1] = st[s][1];
        if (st[s][0] || st[s][1]) rec_stick_from[s] = fr + 1, rec_stick_w[s] = w;
    }
    if (wrote) fflush(rec);
}

static float mouse_sens = -1;              // TOTK_MOUSE_SENS, 0 = mouse camera off; live (cfg_version)
static unsigned mouse_sens_seen;

static float mouse_sens_get(void) {
    unsigned ver = cfg_version();
    if (mouse_sens >= 0 && ver == mouse_sens_seen) return mouse_sens;
    mouse_sens_seen = ver;
    double v = cfg_double("TOTK_MOUSE_SENS");
    mouse_sens = (float)(v < 0 ? 0 : v > 10 ? 10 : v);
    return mouse_sens;
}


// The mouse camera as a right-stick sample, held for the whole presented frame. Under `lock`.
static void mouse_camera(int32_t out[2]) {
    static uint64_t frame = ~0ull, t0;
    static int32_t held[2];
    uint64_t now = plat_now_ns();
    int32_t dx, dy;
    if (!ikb_captured()) {
        ikb_mouse_take(&dx, &dy);
        held[0] = held[1] = 0;
        frame = ~0ull;
    } else {
        uint64_t fr = __atomic_load_n(&gfx_counters.frames, __ATOMIC_RELAXED);
        double age = frame == ~0ull ? 0 : (double)(now - t0) / 1e9;
        if (fr != frame) {
            double dt = age < 0.004 ? 0.004 : age > 0.1 ? 0.1 : age;
            ikb_mouse_take(&dx, &dy);
            input_mouse_stick(dx, dy, dt, mouse_sens_get(), held);
            frame = fr;
            t0 = now;
        } else if (age > 0.1) {
            held[0] = held[1] = 0;   // frames stopped: do not hold a deflection
        }
    }
    out[0] = held[0];
    out[1] = held[1];
}

static float stick_dead = -1;              // TOTK_STICK_DEADZONE as a fraction of full deflection; live (cfg_version)
static unsigned stick_dead_seen;

static float stick_dead_get(void) {
    unsigned ver = cfg_version();
    if (stick_dead >= 0 && ver == stick_dead_seen) return stick_dead;
    stick_dead_seen = ver;
    double v = cfg_double("TOTK_STICK_DEADZONE");
    stick_dead = (float)(v < 0 ? 0 : v > 50 ? 50 : v) / 100.f;
    return stick_dead;
}

// One gamepad stick through the dead zone; SDL's Y grows downwards, the Switch's upwards.
static void stick(SDL_Gamepad *g, SDL_GamepadAxis ax, SDL_GamepadAxis ay, float dead, int32_t out[2]) {
    input_pad_stick(SDL_GetGamepadAxis(g, ax), -(int32_t)SDL_GetGamepadAxis(g, ay), dead, out);
}

static int32_t label_of(SDL_GamepadButtonLabel lb) {
    return lb == SDL_GAMEPAD_BUTTON_LABEL_A          ? PAD_LABEL_A
           : lb == SDL_GAMEPAD_BUTTON_LABEL_B        ? PAD_LABEL_B
           : lb == SDL_GAMEPAD_BUTTON_LABEL_X        ? PAD_LABEL_X
           : lb == SDL_GAMEPAD_BUTTON_LABEL_Y        ? PAD_LABEL_Y
           : lb == SDL_GAMEPAD_BUTTON_LABEL_CROSS    ? PAD_LABEL_CROSS
           : lb == SDL_GAMEPAD_BUTTON_LABEL_CIRCLE   ? PAD_LABEL_CIRCLE
           : lb == SDL_GAMEPAD_BUTTON_LABEL_SQUARE   ? PAD_LABEL_SQUARE
           : lb == SDL_GAMEPAD_BUTTON_LABEL_TRIANGLE ? PAD_LABEL_TRIANGLE
                                                     : PAD_LABEL_NONE;
}

// Under lock: close the gamepads that went away and open every new one (any brand SDL knows: Xbox, PlayStation, Switch,
// generic); logs each change.
static void scan_pads(void) {
    for (int i = npads - 1; i >= 0; i--) {
        if (SDL_GamepadConnected(pads[i].g)) continue;
        rt_log("input: gamepad \"%s\" disconnected", pads[i].name);
        SDL_CloseGamepad(pads[i].g);
        memmove(&pads[i], &pads[i + 1], (size_t)(npads - i - 1) * sizeof *pads);
        npads--;
        pads_version++;
    }
    int n = 0;
    SDL_JoystickID *ids = SDL_GetGamepads(&n);
    for (int i = 0; ids && i < n && npads < MAX_PADS; i++) {
        int open = 0;
        for (int k = 0; k < npads; k++) open |= pads[k].id == ids[i];
        SDL_Gamepad *g = open ? NULL : SDL_OpenGamepad(ids[i]);
        if (!g) continue;
        Pad *p = &pads[npads++];
        memset(p, 0, sizeof *p);
        p->g = g, p->id = ids[i], p->layout = -1;
        const char *name = SDL_GetGamepadName(g);
        snprintf(p->name, sizeof p->name, "%s", name && *name ? name : "Gamepad");
        SDL_GUIDToString(SDL_GetGamepadGUIDForID(ids[i]), p->guid, sizeof p->guid);
        p->rumble = SDL_GetBooleanProperty(SDL_GetGamepadProperties(g), SDL_PROP_GAMEPAD_CAP_RUMBLE_BOOLEAN, false);
        p->east_a = SDL_GetGamepadButtonLabel(g, SDL_GAMEPAD_BUTTON_EAST) == SDL_GAMEPAD_BUTTON_LABEL_A;
        for (int k = 0; k < 4; k++) p->labels[k] = label_of(SDL_GetGamepadButtonLabel(g, pad_sdl[PAD_SOUTH + k]));
        pads_version++;
        int motion = SDL_GamepadHasSensor(g, SDL_SENSOR_GYRO) && SDL_GamepadHasSensor(g, SDL_SENSOR_ACCEL);
        rt_log("input: gamepad \"%s\" (%s, %s, east button %s)", p->name, p->rumble ? "rumble" : "no rumble",
               motion ? "gyro and accelerometer" : "no motion sensors", p->east_a ? "labelled A" : "not labelled A");
    }
    SDL_free(ids);
}

// Under lock: look for gamepads plugged in or out (once a second) and read the open ones' current reports.
static void pads_tick(void) {
    uint64_t now_ms = plat_now_ns() / 1000000u;
    if (now_ms >= next_scan) {
        next_scan = now_ms + RESCAN_MS;
        scan_pads();
    }
    SDL_UpdateGamepads();
}

// Under lock: a gamepad's layout, picked again when a setting or the gamepad changed; logs each change.
static int pad_layout_get(Pad *p) {
    unsigned ver = cfg_version();
    if (p->layout >= 0 && ver == p->layout_seen) return p->layout;
    p->layout_seen = ver;
    int was = p->layout;
    const char *set = cfg_value("TOTK_PAD_LAYOUT");
    p->layout = input_pad_layout_pick(set, p->east_a);
    if (p->layout != was)
        rt_log("input: \"%s\" layout %s (pad_layout = %s)", p->name, p->layout == PAD_LAYOUT_XBOX ? "xbox" : "nintendo", set);
    return p->layout;
}

// ---- Players (TOTK_PLAYER<n>_DEVICE, live): which devices feed which Switch controller
// "auto": the first gamepad no other player has; player 1 also gets the keyboard and mouse unless a player chose them.
// "keyboard": the keyboard and mouse. "pad:<SDL GUID>:<k>": the k-th connected gamepad of that model (input_device_choices
// names them). "none" (player 2): not connected. A device belongs to one player only.
static int pad_owner[MAX_PADS];      // the player each open gamepad feeds, -1 none
static int kb_owner;                 // the player the keyboard and mouse feed, -1 none
static unsigned assign_cfg_seen = ~0u, assign_pads_seen = ~0u;

static void pad_value(int i, char *out, size_t n) {
    int k = 0;
    for (int j = 0; j < i; j++) k += !strcmp(pads[j].guid, pads[i].guid);
    snprintf(out, n, "pad:%s:%d", pads[i].guid, k);
}

static const char *player_device(int p) {
    char key[32];
    snprintf(key, sizeof key, "TOTK_PLAYER%d_DEVICE", p + 1);
    const char *v = cfg_value(key);
    return v && *v ? v : "auto";
}

// Under lock: pad_owner and kb_owner for the current settings and gamepads; logs each change.
static void assign(void) {
    unsigned v = cfg_version();
    if (v == assign_cfg_seen && pads_version == assign_pads_seen) return;
    assign_cfg_seen = v, assign_pads_seen = pads_version;
    const char *dev[INPUT_PLAYERS];
    for (int p = 0; p < INPUT_PLAYERS; p++) dev[p] = player_device(p);
    for (int i = 0; i < MAX_PADS; i++) pad_owner[i] = -1;
    kb_owner = -1;
    for (int p = 0; p < INPUT_PLAYERS; p++) {
        if (!strcmp(dev[p], "keyboard") && kb_owner < 0) kb_owner = p;
        if (strncmp(dev[p], "pad:", 4)) continue;
        for (int i = 0; i < npads; i++) {
            char val[64];
            pad_value(i, val, sizeof val);
            if (pad_owner[i] < 0 && !strcmp(val, dev[p])) { pad_owner[i] = p; break; }
        }
    }
    for (int p = 0; p < INPUT_PLAYERS; p++) {
        if (strcmp(dev[p], "auto")) continue;
        for (int i = 0; i < npads; i++)
            if (pad_owner[i] < 0) { pad_owner[i] = p; break; }
    }
    if (kb_owner < 0 && strcmp(dev[0], "none")) kb_owner = 0;
    char summary[256];
    size_t len = 0;
    for (int p = 0; p < INPUT_PLAYERS; p++) {
        len += (size_t)snprintf(summary + len, sizeof summary - len, "%splayer %d:", p ? "; " : "", p + 1);
        int any = 0;
        if (kb_owner == p) len += (size_t)snprintf(summary + len, sizeof summary - len, " keyboard and mouse"), any = 1;
        for (int i = 0; i < npads && len < sizeof summary; i++)
            if (pad_owner[i] == p)
                len += (size_t)snprintf(summary + len, sizeof summary - len, "%s \"%s\"", any ? " +" : "", pads[i].name), any = 1;
        if (!any && len < sizeof summary) len += (size_t)snprintf(summary + len, sizeof summary - len, " nothing");
        if (len >= sizeof summary) len = sizeof summary - 1;
    }
    static char last[256];
    if (strcmp(summary, last)) {
        snprintf(last, sizeof last, "%s", summary);
        rt_log("input: %s", summary);
    }
    // The menu's labels and layout follow player 1's gamepad.
    int first = -1;
    for (int i = 0; i < npads && first < 0; i++)
        if (pad_owner[i] == 0) first = i;
    plat_xchg32(&pad_layout_shown, first < 0 ? -1 : pad_layout_get(&pads[first]));
    for (int k = 0; k < 4; k++) plat_xchg32(&pad_labels[k], first < 0 ? PAD_LABEL_NONE : pads[first].labels[k]);
}

// The TOTK_BIND_* values last applied, one per line; the tables are built again when one changes (the menu's remapping rows write
// them while the game runs). Under lock.
static unsigned binds_seen = ~0u;
static char binds_applied[8192];

static void binds_refresh(void) {
    unsigned v = cfg_version();
    if (v == binds_seen) return;
    binds_seen = v;
    char now[sizeof binds_applied];
    size_t len = 0;
    for (int p = 0; p < INPUT_PLAYERS; p++)
        for (int a = 0; a < ACTION_COUNT; a++) {
            char var[40];
            input_bind_var(p, a, var, sizeof var);
            const char *s = cfg_get(var);
            len += (size_t)snprintf(now + len, len < sizeof now ? sizeof now - len : 0, "%s\n", s ? s : "");
        }
    if (!strcmp(now, binds_applied)) return;
    memcpy(binds_applied, now, sizeof now);
    input_map_configure();
}

// TOTK_TEST_PAD=<lx>,<ly>,<rx>,<ry> (test runs): an SDL virtual gamepad whose sticks rest at these raw SDL axis values
// (-32768..32767, down is +y), attached before the first scan so it is the gamepad the game reads: drift and full
// deflection go through the same mapping and dead zone as a real pad.
static void test_pad_attach(void) {
    static SDL_Joystick *joy;   // kept open: a virtual joystick's axes are set through an open handle
    const char *e = cfg_get("TOTK_TEST_PAD");
    int v[4];
    if (!e || !*e) return;
    if (sscanf(e, "%d,%d,%d,%d", &v[0], &v[1], &v[2], &v[3]) != 4) {
        rt_log("input: TOTK_TEST_PAD=%s is not <lx>,<ly>,<rx>,<ry>; no test gamepad", e);
        return;
    }
    SDL_VirtualJoystickDesc d;
    SDL_INIT_INTERFACE(&d);
    d.type = SDL_JOYSTICK_TYPE_GAMEPAD;
    d.naxes = SDL_GAMEPAD_AXIS_COUNT;
    d.nbuttons = SDL_GAMEPAD_BUTTON_COUNT;
    d.name = "TotK test gamepad";
    SDL_JoystickID id = SDL_AttachVirtualJoystick(&d);
    joy = id ? SDL_OpenJoystick(id) : NULL;
    if (!joy) {
        rt_log("input: test gamepad not attached (%s)", SDL_GetError());
        return;
    }
    static const SDL_GamepadAxis axes[4] = {SDL_GAMEPAD_AXIS_LEFTX, SDL_GAMEPAD_AXIS_LEFTY, SDL_GAMEPAD_AXIS_RIGHTX,
                                            SDL_GAMEPAD_AXIS_RIGHTY};
    for (int i = 0; i < 4; i++)
        SDL_SetJoystickVirtualAxis(joy, axes[i], (Sint16)(v[i] < -32768 ? -32768 : v[i] > 32767 ? 32767 : v[i]));
    rt_log("input: test gamepad attached, sticks at %d,%d %d,%d", v[0], v[1], v[2], v[3]);
}

static void start(void) {
    binds_refresh();
    const char *e = cfg_get("TOTK_INPUT");
    if (e && !strcmp(e, "none")) { state = -1; rt_log("input: disabled (TOTK_INPUT=none)"); return; }
    if (!SDL_Init(SDL_INIT_GAMEPAD)) { state = -1; rt_log("input: SDL gamepad init failed (%s), keyboard only", SDL_GetError()); }
    else state = 1;
    if (state == 1) test_pad_attach();
    if (state == 1) {
        scan_pads();
        if (!npads) rt_log("input: no gamepad (keyboard only until one is connected)");
    }
    if (state == -1) state = 2;   // keyboard only
}

static void stick_bits(uint64_t *b, int32_t x, int32_t y, int first) {
    if (x < -STICK_DIR) *b |= 1ull << (first + 0);
    if (y > STICK_DIR) *b |= 1ull << (first + 1);
    if (x > STICK_DIR) *b |= 1ull << (first + 2);
    if (y < -STICK_DIR) *b |= 1ull << (first + 3);
}

static void pick(int32_t *x, int32_t *y, int32_t kx, int32_t ky) {
    int64_t a = (int64_t)*x * *x + (int64_t)*y * *y, b = (int64_t)kx * kx + (int64_t)ky * ky;
    if (b > a) { *x = kx; *y = ky; }
}

// Under lock, state 1, after pads_tick: one gamepad's inputs by position (pd[PAD_*] != 0 held), ORed into pd; 0 when it
// is gone.
static int pad_physical(const Pad *pp, uint8_t pd[PAD_COUNT]) {
    SDL_Gamepad *g = pp->g;
    if (!SDL_GamepadConnected(g)) return 0;
    for (int p = PAD_SOUTH; p < PAD_ZL; p++) pd[p] |= SDL_GetGamepadButton(g, pad_sdl[p]);
    pd[PAD_ZL] |= SDL_GetGamepadAxis(g, SDL_GAMEPAD_AXIS_LEFT_TRIGGER) > TRIGGER_ON;
    pd[PAD_ZR] |= SDL_GetGamepadAxis(g, SDL_GAMEPAD_AXIS_RIGHT_TRIGGER) > TRIGGER_ON;
    for (int p = PAD_ZR + 1; p < PAD_COUNT; p++) pd[p] |= SDL_GetGamepadButton(g, pad_sdl[p]);   // false without paddles
    return 1;
}

// Under lock, state 1, after pads_tick: one gamepad's buttons (Switch bits after TOTK_PAD_LAYOUT and the TOTK_BIND_*
// lines) ORed into *b, and its sticks where they are deflected further than sl / sr; 0 when it is gone.
static void pick(int32_t *x, int32_t *y, int32_t kx, int32_t ky);
static int pad_read(Pad *pp, const InputMap *map, uint64_t *b, int32_t sl[2], int32_t sr[2]) {
    uint8_t pd[PAD_COUNT] = {0};
    if (!pad_physical(pp, pd)) return 0;
    *b |= input_map_pad_buttons_m(map, pd, pad_layout_get(pp));
    float dead = stick_dead_get();
    int32_t l[2], r[2];
    stick(pp->g, SDL_GAMEPAD_AXIS_LEFTX, SDL_GAMEPAD_AXIS_LEFTY, dead, l);
    stick(pp->g, SDL_GAMEPAD_AXIS_RIGHTX, SDL_GAMEPAD_AXIS_RIGHTY, dead, r);
    pick(&sl[0], &sl[1], l[0], l[1]);
    pick(&sr[0], &sr[1], r[0], r[1]);
    return 1;
}

// The optional diagnostic feed works with input=none as well as live controls,
// so scripted progression does not need desktop focus. Default: no file I/O.
static void feed_apply(uint64_t *buttons, int32_t left[2], int32_t right[2]) {
    static int initialized;
    static const char *path;
    static uint64_t next_poll;
    static int baseline_read;
    static InputFeed feed;
    if (!initialized) {
        path = cfg_get("TOTK_INPUT_FEED"); initialized = 1;
        if (path && *path) rt_log("input: diagnostic feed %s (commands expire within 10 seconds)", path);
    }
    if (!path || !*path) return;
    uint64_t now = plat_now_ns();
    if (now >= next_poll) {
        next_poll = now + 33333333;
        FILE *f = fopen(path, "r");
        if (f) {
            char line[256];
            if (fgets(line, sizeof line, f) && fgetc(f) == EOF && input_feed_accept(&feed, line, now)) {
                if (!baseline_read) {
                    baseline_read = 1;
                    feed.until = now; // a previous process's final command must not replay on launch
                    rt_log("input: diagnostic feed baseline %llu; waiting for a new command", (unsigned long long)feed.sequence);
                } else {
                if (feed.fps) {
                    const char *rate_experiment = cfg_get("TOTK_EXPERIMENTAL_RATE");
                    if ((!frame_step_native_rate() && (!rate_experiment || strcmp(rate_experiment, "1"))) || cfg_from_env("TOTK_FPS_LIMIT"))
                        rt_log("input: FPS experiment rejected; enable EXPERIMENTAL_RATE and leave FPS_LIMIT free of an environment override");
                    else {
                        char value[16]; snprintf(value, sizeof value, "%u", feed.fps);
                        cfg_set("TOTK_FPS_LIMIT", value);
                    }
                }
                if (feed.width) {
                    if (cfg_from_env("TOTK_WINDOW_SIZE")) rt_log("input: output resize rejected; WINDOW_SIZE is fixed by the environment");
                    else {
                        char value[32]; snprintf(value, sizeof value, "%ux%u", feed.width, feed.height);
                        cfg_set("TOTK_WINDOW_SIZE", value);
                        rt_log("input: output resolution %s requested; internal game resolution unchanged", value);
                    }
                }
                rt_log("input: diagnostic command %llu buttons=%#llx left=%d,%d right=%d,%d hold_ms=%llu",
                    (unsigned long long)feed.sequence, (unsigned long long)feed.buttons,
                    feed.axis[0], feed.axis[1], feed.axis[2], feed.axis[3],
                    (unsigned long long)((feed.until - now) / 1000000));
                }
            }
            fclose(f);
        }
    }
    input_feed_merge(&feed, now, buttons, left, right);
}

// Player p's controller: the gamepads and, when it owns them, the keyboard and mouse that feed it (assign). Returns 1
// when the player has a device (player 1 always counts as connected). Script, feed and recording belong to player 1.
static int poll_player(int p, uint64_t *buttons, int32_t *lx, int32_t *ly, int32_t *rx, int32_t *ry) {
    uint64_t b = 0;
    int32_t sl[2] = {0, 0}, sr[2] = {0, 0};
    int connected = p == 0;
    plat_mutex_lock(&lock);
    if (!state) start();
    if (state > 0) {
        binds_refresh();
        if (state == 1) pads_tick();
        assign();
        if (state == 1)
            for (int i = 0; i < npads; i++)
                if (pad_owner[i] == p) connected |= 1, pad_read(&pads[i], input_map_player(p), &b, sl, sr);
        if (kb_owner == p) connected = 1;
        if (kb_owner == p && ikb_foreground()) {
            uint8_t down[KEY_COUNT] = {0};
            // Mouse buttons count only while the cursor is captured, and a button already held when the capture began
            // (the click that refocused the window) is ignored until it is released.
            static int was_captured;
            static uint8_t mouse_ignore[KEY_COUNT];
            int cap = ikb_captured();
            for (int k = 1; k < KEY_COUNT; k++) {
                down[k] = ikb_down(k);
                if (k < KEY_MOUSE_L || k > KEY_MOUSE_X2) continue;
                if (cap && !was_captured) mouse_ignore[k] = down[k];
                if (!down[k]) mouse_ignore[k] = 0;
                if (!cap || mouse_ignore[k]) down[k] = 0;
            }
            was_captured = cap;
            const InputMap *map = input_map_player(p);
            b |= input_map_buttons_m(map, down);
            int32_t k[2];
            input_map_stick_m(map, down, 0, k);
            pick(&sl[0], &sl[1], k[0], k[1]);
            input_map_stick_m(map, down, 1, k);
            pick(&sr[0], &sr[1], k[0], k[1]);
            mouse_camera(k);
            pick(&sr[0], &sr[1], k[0], k[1]);
        }
    }
    if (p == 0) {
        script_apply(&b, sl, sr);
        feed_apply(&b, sl, sr);
        rec_apply(b, sl, sr);
    }
    // Minus and Plus held together (gamepad or keys) open or close the menu and never reach the game; either player can
    static int combo_held[INPUT_PLAYERS];
    int32_t st[4] = {sl[0], sl[1], sr[0], sr[1]};
    int toggle = input_menu_gate(&combo_held[p], menu_is_open(), &b, st);
    static int overlay_held[INPUT_PLAYERS];   // L3+R3 step the performance overlay (F10)
    int overlay = input_overlay_gate(&overlay_held[p], &b);
    sl[0] = st[0], sl[1] = st[1], sr[0] = st[2], sr[1] = st[3];
    plat_mutex_unlock(&lock);
    if (toggle) menu_toggle();
    if (overlay) menu_perf_cycle();
    stick_bits(&b, sl[0], sl[1], B_SL_LEFT);
    stick_bits(&b, sr[0], sr[1], B_SR_LEFT);
    *buttons = b; *lx = sl[0]; *ly = sl[1]; *rx = sr[0]; *ry = sr[1];
    return connected;
}

// Text entry (swkbd_*.c): while it is open the game reads no input, as behind the keyboard applet; after it closes
// the game reads none until every button and stick direction is released, so the press that closed it stays there.
// g_block: 0 off, 1 text entry open, 2 closed, waiting for the release. The cursor is released while it is open.
static volatile int32_t g_block;
static volatile int32_t g_menu;   // the in-game menu, the same way (input_menu)

int input_cursor_allowed(void) {
    const char *in = cfg_get("TOTK_INPUT");
    return g_block != 1 && g_menu != 1 && mouse_sens_get() > 0 && !(in && !strcmp(in, "none"));
}

void input_block(int on) {
    plat_xchg32(&g_block, on ? 1 : 2);
    ikb_cursor_update();
}

// The in-game menu (ui/menu.cpp) blocks the game's input the same way: g_menu 0 closed, 1 open, 2 closed, waiting for
// the release. The cursor is released while it is open, for the menu's mouse.
void input_menu(int open) {
    plat_xchg32(&g_menu, open ? 1 : 2);
    if (!open) ikb_menu_closed();   // the capture before Esc comes back
    ikb_cursor_update();
}

// 1 while flag (g_block, g_menu) blocks the game's input; state 2 ends at the first poll with nothing held.
static int blocked(volatile int32_t *flag, uint64_t buttons) {
    int32_t k = *flag;
    return k && !(k == 2 && !buttons && plat_cas32(flag, 0, 2) == 2);
}

int input_poll_player(int p, uint64_t *buttons, int32_t *lx, int32_t *ly, int32_t *rx, int32_t *ry) {
    if (p < 0 || p >= INPUT_PLAYERS) { *buttons = 0; *lx = *ly = *rx = *ry = 0; return 0; }
    int connected = poll_player(p, buttons, lx, ly, rx, ry);
    // Player 1's releases end a text entry or menu block (blocked); the other players stay quiet until then.
    int quiet = p == 0 ? blocked(&g_block, *buttons) | blocked(&g_menu, *buttons) : g_block != 0 || g_menu != 0;
    if (quiet) { *buttons = 0; *lx = *ly = *rx = *ry = 0; }
    return connected;
}

void input_poll(uint64_t *buttons, int32_t *lx, int32_t *ly, int32_t *rx, int32_t *ry) {
    input_poll_player(0, buttons, lx, ly, rx, ry);
}

void input_poll_ui(uint64_t *buttons, int32_t *lx, int32_t *ly, int32_t *rx, int32_t *ry) {
    poll_player(0, buttons, lx, ly, rx, ry);
}

int input_player_connected(int p) {
    if (p == 0) return 1;
    if (p < 0 || p >= INPUT_PLAYERS) return 0;
    plat_mutex_lock(&lock);
    if (!state) start();
    if (state == 1) pads_tick();
    if (state > 0) assign();
    int connected = kb_owner == p;
    for (int i = 0; i < npads && state == 1; i++) connected |= pad_owner[i] == p;
    plat_mutex_unlock(&lock);
    return connected;
}

int input_device_choices(InputDeviceChoice *out, int max) {
    int n = 0;
    plat_mutex_lock(&lock);
    if (!state) start();
    if (state == 1) pads_tick();
    for (int i = 0; i < npads && n < max; i++, n++) {
        pad_value(i, out[n].value, sizeof out[n].value);
        int same = 0, k = 0;
        for (int j = 0; j < npads; j++) same += !strcmp(pads[j].guid, pads[i].guid), k += j < i && !strcmp(pads[j].guid, pads[i].guid);
        if (same > 1) snprintf(out[n].name, sizeof out[n].name, "%s #%d", pads[i].name, k + 1);
        else snprintf(out[n].name, sizeof out[n].name, "%s", pads[i].name);
    }
    plat_mutex_unlock(&lock);
    return n;
}

// The menu: any gamepad navigates it.
int input_pad_read(uint64_t *buttons, int32_t *lx, int32_t *ly) {
    uint64_t b = 0;
    int32_t sl[2] = {0, 0}, sr[2] = {0, 0};
    int ok = 0;
    plat_mutex_lock(&lock);
    if (!state) start();
    if (state > 0) binds_refresh();
    if (state == 1) {
        pads_tick();
        for (int i = 0; i < npads; i++) ok |= pad_read(&pads[i], &input_map, &b, sl, sr);
    }
    plat_mutex_unlock(&lock);
    *buttons = b;
    *lx = sl[0];
    *ly = sl[1];
    return ok;
}

uint16_t rumble_motor(float a, float curve, float strength) {
    if (!(a > 0) || !(strength > 0)) return 0;
    float v = powf(a > 1 ? 1 : a, curve) * strength * 65535.0f + 0.5f;
    return v >= 65535.0f ? 65535 : (uint16_t)v;
}

int input_pad_layout(void) {
    return (int)plat_cas32(&pad_layout_shown, 0, 0);
}

int input_pad_label(int pad_id) {
    if (pad_id < PAD_SOUTH || pad_id > PAD_NORTH) return PAD_LABEL_NONE;
    return (int)plat_cas32(&pad_labels[pad_id - PAD_SOUTH], 0, 0);
}

void input_map_snapshot_player(int player, InputMap *m) {
    plat_mutex_lock(&lock);
    if (!state) start();
    if (state > 0) binds_refresh();
    *m = *input_map_player(player);
    plat_mutex_unlock(&lock);
}

void input_map_snapshot(InputMap *m) { input_map_snapshot_player(0, m); }

int input_capture(int reset, int *id) {
    static uint8_t prev_k[KEY_COUNT], prev_p[PAD_COUNT];
    static int prev_esc;
    uint8_t k[KEY_COUNT] = {0}, p[PAD_COUNT] = {0};
    int fg = ikb_foreground();
    for (int i = 1; i < KEY_COUNT; i++) k[i] = fg && ikb_down(i);
    int esc = fg && ikb_escape();
    plat_mutex_lock(&lock);
    if (!state) start();
    if (state == 1) {
        pads_tick();
        for (int i = 0; i < npads; i++) pad_physical(&pads[i], p);   // any gamepad can be the one being bound
    }
    plat_mutex_unlock(&lock);
    int got = INPUT_CAPTURE_NONE;
    if (!reset) {
        if (esc && !prev_esc) got = INPUT_CAPTURE_CANCEL;
        for (int i = 1; i < KEY_COUNT && !got; i++)
            if (k[i] && !prev_k[i]) got = INPUT_CAPTURE_KEY, *id = i;
        for (int i = 1; i < PAD_COUNT && !got; i++)
            if (p[i] && !prev_p[i]) got = INPUT_CAPTURE_PAD, *id = i;
    }
    memcpy(prev_k, k, sizeof k);
    memcpy(prev_p, p, sizeof p);
    prev_esc = esc;
    return got;
}

int input_rumble_triggers_enabled(void) {
    return cfg_bool("TOTK_RUMBLE_TRIGGERS");
}

int input_rumble_player(int player, uint16_t low, uint16_t high, uint32_t ms) {
    int ok = 0;
    static int triggers = -1;
    static unsigned seen;
    if (triggers < 0 || seen != cfg_version()) seen = cfg_version(), triggers = input_rumble_triggers_enabled();
    plat_mutex_lock(&lock);
    if (state == 1) assign();
    for (int i = 0; state == 1 && i < npads; i++) {
        if (pad_owner[i] != player || !pads[i].rumble) continue;
        ok |= SDL_RumbleGamepad(pads[i].g, low, high, ms);
        if (triggers) SDL_RumbleGamepadTriggers(pads[i].g, high, high, ms);   // unsupported pads return false: ignored
    }
    plat_mutex_unlock(&lock);
    return ok;
}

int input_rumble(uint16_t low, uint16_t high, uint32_t ms) { return input_rumble_player(0, low, high, ms); }

// SDL gamepad sensor axes (SDL_sensor.h, a gamepad held in front of you): x to the right, y up out of the face (lying
// flat face up, +y points at the ceiling), z toward the player (out of the bottom edge). The accelerometer reads the
// push against gravity in m/s^2 (lying flat: y = +9.81); the gyro reads rad/s, counter-clockwise positive (right hand).
// Switch six-axis axes (nn::hid, as MacNativeGames' GCMotion feed and its Pro Controller HID path use them): x to the
// right, y toward the top edge (away from the player), z out of the face; acceleration as gravity reads (lying flat:
// z = -1 G). So Switch x = SDL x, Switch y = -SDL z, Switch z = SDL y: a rotation (right-handed to right-handed), so the
// gyro keeps its sense, while the acceleration is also negated (push -> gravity) and scaled to G.
void input_motion_axes(const float a[3], const float g[3], double acc[3], double gyro[3]) {
    acc[0] = -a[0] / SDL_STANDARD_GRAVITY;
    acc[1] = a[2] / SDL_STANDARD_GRAVITY;
    acc[2] = -a[1] / SDL_STANDARD_GRAVITY;
    gyro[0] = g[0];
    gyro[1] = -g[2];
    gyro[2] = g[1];
}

// Under lock: enable a gamepad's sensors the first time; 1 if it has both and they are on.
static int sensors_ready(Pad *pp) {
    if (pp->sensor_set) return pp->sensor_ok;
    pp->sensor_set = 1;
    // Nothing drains the SDL event queue: no event per sample (the data still updates, SDL_SendJoystickSensor)
    SDL_SetEventEnabled(SDL_EVENT_GAMEPAD_SENSOR_UPDATE, false);
    SDL_Gamepad *g = pp->g;
    pp->sensor_ok = SDL_GamepadHasSensor(g, SDL_SENSOR_GYRO) && SDL_GamepadHasSensor(g, SDL_SENSOR_ACCEL) &&
                    SDL_SetGamepadSensorEnabled(g, SDL_SENSOR_GYRO, true) &&
                    SDL_SetGamepadSensorEnabled(g, SDL_SENSOR_ACCEL, true);
    if (pp->sensor_ok) rt_log("input: \"%s\" motion sensors on (gyro %.0f Hz, accelerometer %.0f Hz)", pp->name,
                              (double)SDL_GetGamepadSensorDataRate(g, SDL_SENSOR_GYRO),
                              (double)SDL_GetGamepadSensorDataRate(g, SDL_SENSOR_ACCEL));
    else rt_log("input: \"%s\" has no gyro and accelerometer: its six-axis sensor reads a controller at rest", pp->name);
    return pp->sensor_ok;
}

// Each player's six-axis sensor: player 1's is motion_global() (the fusion ov_input.c always read).
MotionFusion *input_motion_player(int p) {
    static MotionFusion *others[INPUT_PLAYERS];
    if (p <= 0 || p >= INPUT_PLAYERS) return motion_global();
    MotionFusion *m = __atomic_load_n(&others[p], __ATOMIC_ACQUIRE);
    if (m) return m;
    MotionFusion *fresh = motion_new(), *expected = NULL;
    if (!__atomic_compare_exchange_n(&others[p], &expected, fresh, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return expected;
    return fresh;
}

int input_motion_enabled(void) {
    static int on = -1;
    static unsigned seen;
    unsigned v = cfg_version();
    if (on < 0 || v != seen) {
        seen = v;
        on = cfg_bool("TOTK_MOTION");
    }
    return on;
}

// 250 Hz: the gamepads update (reports read, rumble expiry) and the newest gyro and accelerometer sample goes into the
// fusion. Without a gamepad with sensors, or with motion off (TOTK_MOTION=0, live), nothing is fed and the six-axis
// sensor reads at rest.
static void motion_main(void *arg) {
    (void)arg;
    for (;;) {
        plat_wait_ns(4000000);
        float a[INPUT_PLAYERS][3], g[INPUT_PLAYERS][3];
        int have[INPUT_PLAYERS] = {0};
        plat_mutex_lock(&lock);
        if (npads) {
            SDL_UpdateGamepads();
            assign();
            // each player's sensor follows the first of its gamepads that has one
            for (int i = 0; i < npads; i++) {
                int p = pad_owner[i];
                if (p < 0 || have[p] || !SDL_GamepadConnected(pads[i].g)) continue;
                have[p] = sensors_ready(&pads[i]) && SDL_GetGamepadSensorData(pads[i].g, SDL_SENSOR_ACCEL, a[p], 3) &&
                          SDL_GetGamepadSensorData(pads[i].g, SDL_SENSOR_GYRO, g[p], 3);
            }
        }
        plat_mutex_unlock(&lock);
        if (!input_motion_enabled()) continue;
        for (int p = 0; p < INPUT_PLAYERS; p++) {
            if (!have[p]) continue;
            double acc[3], gyro[3];
            input_motion_axes(a[p], g[p], acc, gyro);
            motion_feed(input_motion_player(p), acc, gyro, kern_now_ns());
        }
    }
}

void input_motion_start(void) {
    plat_mutex_lock(&lock);
    if (!state) start();
    if (!motion_thread) {
        if (!input_motion_enabled()) rt_log("input: motion off (TOTK_MOTION=0); it can be turned on while the game runs");
        if (state != 1) motion_thread = -1;
        else {
            if (!plat_thread_spawn(motion_main, NULL, "motion", 0)) rt_fatal("input: cannot start the motion thread");
            motion_thread = 1;
        }
    }
    plat_mutex_unlock(&lock);
}
