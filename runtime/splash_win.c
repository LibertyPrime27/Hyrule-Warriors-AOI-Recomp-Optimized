// HWAoI loading window: shown from process start until the game presents its first frame.
// The game window only exists once the guest initialises graphics, and the boot prewarm (shaders and pipelines recorded
// in earlier runs, vk_pipeline_prewarm) runs before that. Without this window a launch showed nothing for several
// seconds, then a black game window. The loading window has the game window's client size, centred, a title, a status
// line and a progress bar fed by vk_pipeline_prewarm_progress. It closes itself at the first present (rt_frame_presented)
// so the game window takes over from it directly.
// Enabled with HWAOI_SPLASH=1, which the interactive launcher sets; probes and tests never show it. boot.c has weak no-op
// fallbacks for platforms without this file.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include "cfg.h"
#include "host.h"
#include "platform/platform.h"
#include "gfx_wsys.h"
#include "vk.h"
#include "game_target.h"

#define SPLASH_CLASS L"HWAoISplash"
#define TICK_MS 33

static HWND g_splash;
static volatile LONG g_closed;    // splash_stop ran (possibly before the window existed)
static HANDLE g_thread;
static ULONGLONG g_t0;

// Displayed progress 0..1 and the status line. Shader translation has no count, so it advances with time towards 30%;
// pipeline builds fill the rest by count. After the prewarm the game itself is still starting: hold at 100%.
static double progress(wchar_t *line, size_t cap) {
    int stage;
    uint32_t done, total;
    vk_pipeline_prewarm_progress(&stage, &done, &total);
    double secs = (double)(GetTickCount64() - g_t0) / 1000.0;
    if (stage <= 1) {
        swprintf(line, cap, L"Preparing shaders\x2026");
        return stage == 0 ? 0.02 : 0.30 * (1.0 - 1.0 / (1.0 + secs / 4.0));
    }
    if (stage == 2 && total) {
        swprintf(line, cap, L"Building graphics pipelines\x2026  %u / %u", done, total);
        return 0.30 + 0.70 * (double)done / (double)total;
    }
    swprintf(line, cap, L"Starting the game\x2026");
    return 1.0;
}

static void paint(HWND h) {
    PAINTSTRUCT ps;
    HDC dc = BeginPaint(h, &ps);
    RECT rc;
    GetClientRect(h, &rc);
    int w = rc.right, ht = rc.bottom;
    HDC mem = CreateCompatibleDC(dc);
    HBITMAP bmp = CreateCompatibleBitmap(dc, w, ht);
    HGDIOBJ old = SelectObject(mem, bmp);
    HBRUSH black = CreateSolidBrush(RGB(0, 0, 0));
    FillRect(mem, &rc, black);
    DeleteObject(black);

    SetBkMode(mem, TRANSPARENT);
    int title_px = ht / 14 > 18 ? ht / 14 : 18, text_px = ht / 32 > 12 ? ht / 32 : 12;
    HFONT title = CreateFontW(-title_px, 0, 0, 0, FW_SEMIBOLD, 0, 0, 0, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0,
                              L"Segoe UI");
    HFONT text = CreateFontW(-text_px, 0, 0, 0, FW_NORMAL, 0, 0, 0, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0,
                             L"Segoe UI");
    HGDIOBJ of = SelectObject(mem, title);
    SetTextColor(mem, RGB(236, 228, 206));
    RECT tr = {0, ht * 38 / 100, w, ht * 38 / 100 + title_px * 2};
    DrawTextW(mem, RT_GAME_TITLE_W, -1, &tr, DT_CENTER | DT_SINGLELINE | DT_VCENTER);

    wchar_t line[128];
    double p = progress(line, sizeof line / sizeof *line);
    int bw = w * 46 / 100, bh = ht / 90 > 4 ? ht / 90 : 4, bx = (w - bw) / 2, by = ht * 62 / 100;
    RECT track = {bx, by, bx + bw, by + bh}, fill = {bx, by, bx + (int)(bw * (p < 0 ? 0 : p > 1 ? 1 : p)), by + bh};
    HBRUSH tb = CreateSolidBrush(RGB(48, 46, 42)), fb = CreateSolidBrush(RGB(214, 180, 92));
    FillRect(mem, &track, tb);
    FillRect(mem, &fill, fb);
    DeleteObject(tb);
    DeleteObject(fb);

    SelectObject(mem, text);
    SetTextColor(mem, RGB(160, 154, 140));
    RECT lr = {0, by + bh + text_px, w, by + bh + text_px * 3};
    DrawTextW(mem, line, -1, &lr, DT_CENTER | DT_SINGLELINE | DT_TOP);
    SelectObject(mem, of);
    DeleteObject(title);
    DeleteObject(text);

    BitBlt(dc, 0, 0, w, ht, mem, 0, 0, SRCCOPY);
    SelectObject(mem, old);
    DeleteObject(bmp);
    DeleteDC(mem);
    EndPaint(h, &ps);
}

static LRESULT CALLBACK proc(HWND h, UINT m, WPARAM w, LPARAM l) {
    switch (m) {
    case WM_PAINT: paint(h); return 0;
    case WM_ERASEBKGND: return 1;   // paint() fills everything; no white flash
    case WM_TIMER: InvalidateRect(h, NULL, FALSE); return 0;
    case WM_CLOSE: DestroyWindow(h); return 0;
    case WM_DESTROY: KillTimer(h, 1); PostQuitMessage(0); return 0;
    }
    return DefWindowProcW(h, m, w, l);
}

static DWORD WINAPI splash_main(LPVOID unused) {
    (void)unused;
    HINSTANCE inst = GetModuleHandleW(NULL);
    WNDCLASSEXW wc = {.cbSize = sizeof wc, .lpfnWndProc = proc, .hInstance = inst, .lpszClassName = SPLASH_CLASS,
                      .hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_APPSTARTING), .hbrBackground = NULL,
                      .hIcon = LoadIconW(inst, MAKEINTRESOURCEW(1))};
    RegisterClassExW(&wc);
    int cw = 1280, ch = 720;
    gfx_cfg_window_size(&cw, &ch);
    HMONITOR mon = MonitorFromPoint((POINT){0, 0}, MONITOR_DEFAULTTOPRIMARY);
    MONITORINFO mi = {.cbSize = sizeof mi};
    GetMonitorInfoW(mon, &mi);
    RECT wa = mi.rcWork;
    int x = wa.left + ((wa.right - wa.left) - cw) / 2, y = wa.top + ((wa.bottom - wa.top) - ch) / 2;
    HWND h = CreateWindowExW(WS_EX_APPWINDOW, SPLASH_CLASS, RT_GAME_TITLE_W, WS_POPUP,
                             x, y, cw, ch, NULL, NULL, inst, NULL);
    if (!h) {
        rt_log("splash: no loading window (error %lu)", GetLastError());
        return 0;
    }
    g_splash = h;
    if (InterlockedCompareExchange(&g_closed, 0, 0)) {   // the game presented before the window existed
        DestroyWindow(h);
        return 0;
    }
    ShowWindow(h, SW_SHOWNORMAL);
    UpdateWindow(h);
    SetTimer(h, 1, TICK_MS, NULL);
    MSG msg;
    while (GetMessageW(&msg, NULL, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    g_splash = NULL;
    return 0;
}

void splash_start(void) {
    const char *e = getenv("HWAOI_SPLASH");
    if (!e || strcmp(e, "1") || g_thread) return;
    g_t0 = GetTickCount64();
    g_thread = CreateThread(NULL, 0, splash_main, NULL, 0, NULL);
    if (g_thread) rt_log("splash: loading window shown until the first present");
}

void splash_stop(void) {
    if (!g_thread || InterlockedExchange(&g_closed, 1)) return;
    HWND h = g_splash;
    if (h) PostMessageW(h, WM_CLOSE, 0, 0);
    rt_log("splash: closed at the first present, %.1f s after it opened", (double)(GetTickCount64() - g_t0) / 1000.0);
}
