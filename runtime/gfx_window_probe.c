// --probe-window: presents 300 frames of a cycling clear colour through vk_present_image, reads the swapchain image back
// and compares every pixel, measures the vsync rate, flips the focus state and drives the cursor release with synthetic
// window events (the window back end's wsys_probe_*, gfx_wsys.h) and resizes the window. Run with TOTK_VK_VALIDATION=1
// to count validation errors.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cfg.h"
#include "gfx.h"
#include "gfx_wsys.h"
#include "input.h"
#include "host.h"
#include "ui/menu.h"
#include "vk.h"
#include "platform/platform.h"

#define SRC_W 640
#define SRC_H 360
#define FRAMES 300

typedef struct {
    VkImage img;
    VmaAllocation alloc;
    VkCommandPool pool;
    VkCommandBuffer cb;
    VkFence fence;
    VkBuffer upload;
    VmaAllocation upload_alloc;
    uint8_t *upload_ptr;
    int width, height;
    VkFormat format;
    VkImageLayout layout;
} Source;

static int fail(const char *what) {
    printf("probe-window: FAILED (%s)\n", what);
    return 1;
}

static void barrier(VkCommandBuffer cb, VkImage img, VkImageLayout from, VkImageLayout to, VkPipelineStageFlags2 s0,
                    VkAccessFlags2 a0, VkPipelineStageFlags2 s1, VkAccessFlags2 a1) {
    VkImageMemoryBarrier2 b = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER_2, .srcStageMask = s0, .srcAccessMask = a0,
                               .dstStageMask = s1, .dstAccessMask = a1, .oldLayout = from, .newLayout = to,
                               .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
                               .image = img, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    VkDependencyInfo d = {.sType = VK_STRUCTURE_TYPE_DEPENDENCY_INFO, .imageMemoryBarrierCount = 1, .pImageMemoryBarriers = &b};
    vkCmdPipelineBarrier2(cb, &d);
}

static int source_create_format(Source *s, VkFormat format, int width, int height) {
    VkCtx *c = vk_ctx();
    VkImageCreateInfo ii = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D,
                            .format = format, .extent = {(uint32_t)width, (uint32_t)height, 1}, .mipLevels = 1, .arrayLayers = 1,
                            .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
                            .usage = VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT,
                            .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED};
    VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE};
    if (vmaCreateImage(c->vma, &ii, &ai, &s->img, &s->alloc, NULL) != VK_SUCCESS) return 0;
    s->width = width; s->height = height; s->format = format;
    VkCommandPoolCreateInfo pi = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                  .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, .queueFamilyIndex = c->gfx_family};
    if (vkCreateCommandPool(c->device, &pi, NULL, &s->pool) != VK_SUCCESS) return 0;
    VkCommandBufferAllocateInfo ba = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = s->pool,
                                      .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    if (vkAllocateCommandBuffers(c->device, &ba, &s->cb) != VK_SUCCESS) return 0;
    VkFenceCreateInfo fi = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    return vkCreateFence(c->device, &fi, NULL, &s->fence) == VK_SUCCESS;
}

static int source_create(Source *s) { return source_create_format(s, VK_FORMAT_R8G8B8A8_UNORM, SRC_W, SRC_H); }

static void source_destroy(Source *s) {
    VkCtx *c = vk_ctx();
    if (s->fence) vkDestroyFence(c->device, s->fence, NULL);
    if (s->pool) vkDestroyCommandPool(c->device, s->pool, NULL);
    if (s->upload) vmaDestroyBuffer(c->vma, s->upload, s->upload_alloc);
    if (s->img) vmaDestroyImage(c->vma, s->img, s->alloc);
}

// Clears the source to rgba and leaves it in TRANSFER_SRC_OPTIMAL; the GPU work is waited for here, which is stricter
// than the vk_present_image contract needs.
static int source_clear(Source *s, int first, const float rgba[4]) {
    VkCtx *c = vk_ctx();
    vkResetCommandBuffer(s->cb, 0);
    VkCommandBufferBeginInfo bi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                                   .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    vkBeginCommandBuffer(s->cb, &bi);
    barrier(s->cb, s->img, first ? VK_IMAGE_LAYOUT_UNDEFINED : VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_PIPELINE_STAGE_2_TRANSFER_BIT, first ? 0 : VK_ACCESS_2_TRANSFER_READ_BIT,
            VK_PIPELINE_STAGE_2_TRANSFER_BIT, VK_ACCESS_2_TRANSFER_WRITE_BIT);
    VkClearColorValue cv;
    memcpy(cv.float32, rgba, sizeof cv.float32);
    VkImageSubresourceRange rng = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    vkCmdClearColorImage(s->cb, s->img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &cv, 1, &rng);
    barrier(s->cb, s->img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_PIPELINE_STAGE_2_TRANSFER_BIT, VK_ACCESS_2_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_2_TRANSFER_BIT,
            VK_ACCESS_2_TRANSFER_READ_BIT);
    vkEndCommandBuffer(s->cb);
    VkCommandBufferSubmitInfo cbi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_SUBMIT_INFO, .commandBuffer = s->cb};
    VkSubmitInfo2 si = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO_2, .commandBufferInfoCount = 1, .pCommandBufferInfos = &cbi};
    vk_queue_lock();
    VkResult r = vkQueueSubmit2(c->gfx_queue, 1, &si, s->fence);
    vk_queue_unlock();
    if (r != VK_SUCCESS) return 0;
    r = vkWaitForFences(c->device, 1, &s->fence, VK_TRUE, 5000000000ull);
    vkResetFences(c->device, 1, &s->fence);
    return r == VK_SUCCESS;
}

static uint8_t to_byte(float f) { return (uint8_t)(f * 255.0f + 0.5f); }

// Presents one colour and checks every pixel of the readback. Returns 0 when it matches.
static int present_and_check(Source *s, int first, int i, int *w_out, int *h_out) {
    float rgba[4] = {(float)((i * 7) & 255) / 255.0f, (float)((i * 13) & 255) / 255.0f, (float)((i * 29) & 255) / 255.0f, 1.0f};
    if (!source_clear(s, first, rgba)) return 1;
    vk_present_image(s->img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_FORMAT_R8G8B8A8_UNORM, SRC_W, SRC_H, NULL);
    const uint8_t *px;
    int w, h;
    VkFormat fmt;
    if (!vk_present_captured(&px, &w, &h, &fmt)) { printf("probe-window: frame %d was not presented\n", i); return 2; }
    uint8_t rgb[3] = {to_byte(rgba[0]), to_byte(rgba[1]), to_byte(rgba[2])};
    uint8_t want[4];
    if (fmt == VK_FORMAT_B8G8R8A8_UNORM) { want[0] = rgb[2]; want[1] = rgb[1]; want[2] = rgb[0]; }
    else if (fmt == VK_FORMAT_R8G8B8A8_UNORM) { want[0] = rgb[0]; want[1] = rgb[1]; want[2] = rgb[2]; }
    else { printf("probe-window: swapchain format %d is not UNORM RGBA/BGRA\n", (int)fmt); return 3; }
    want[3] = 255;
    uint32_t wv, black = 0xff000000u;   // opaque black in both byte orders
    memcpy(&wv, want, 4);
    int r[4];   // the picture's rectangle; bars of black around it when the window's shape differs
    gfx_fit_rect(SRC_W, SRC_H, w, h, r);
    const uint32_t *p = (const uint32_t *)px;
    for (long k = 0, n = (long)w * h; k < n; k++) {
        int x = (int)(k % w), y = (int)(k / w);
        uint32_t e = x >= r[0] && x < r[0] + r[2] && y >= r[1] && y < r[1] + r[3] ? wv : black;
        if (p[k] != e) {
            printf("probe-window: frame %d pixel %ld (%d, %d) is %08x, expected %08x\n", i, k, x, y, p[k], e);
            return 4;
        }
    }
    *w_out = w;
    *h_out = h;
    return 0;
}

int probe_window(void) {
    if (!menu_font_check()) return fail("the menu's font check takes a damaged font or refuses a good one");
    gfx_window_start();
    VkCtx *c = vk_ctx();
    if (!c) return fail("no Vulkan device");
    vk_present_capture(1);
    Source s = {0};
    if (!source_create(&s)) return fail("source image");

    int w = 0, h = 0;
    for (int i = 0; i < FRAMES; i++)
        if (present_and_check(&s, i == 0, i, &w, &h)) return fail("presented pixels differ");
    printf("probe-window: %d presents, readback %dx%d exact\n", FRAMES, w, h);

    // Focus: synthetic focus loss and gain, delivered synchronously on the window thread.
    unsigned m0 = gfx_window_focus_messages();
    wsys_probe_focus(0);
    int off = gfx_window_focused();
    wsys_probe_focus(1);
    int on = gfx_window_focused();
    unsigned m1 = gfx_window_focus_messages();
    if (off != 0 || on != 1 || m1 - m0 != 2) {
        printf("probe-window: focus off=%d on=%d messages=%u\n", off, on, m1 - m0);
        return fail("focus");
    }
    printf("probe-window: focus off/on pushed 2 messages (15)\n");

    // Cursor release: Esc releases, a client click recaptures, a title bar activation releases (where the window system
    // reports one). The probe window may not be the foreground window, so this checks the release flag, not the capture.
    wsys_probe_escape();
    int esc = input_cursor_released();
    wsys_probe_click(0);
    int click = input_cursor_released();
    wsys_probe_click(1);
    int client = input_cursor_released();
    int caption = wsys_probe_click(2) ? input_cursor_released() : 1;
    wsys_probe_focus(0);
    wsys_probe_focus(1);
    int back = input_cursor_released();
    if (menu_is_open()) menu_toggle();   // Esc opened the menu: the presents below compare the bare game image
    if (!esc || click || client || !caption || back) {
        printf("probe-window: released esc=%d click=%d client=%d caption=%d reactivated=%d\n", esc, click, client, caption, back);
        return fail("cursor release");
    }
    printf("probe-window: cursor released by Esc and title bar activation, recaptured by a click\n");

    // Resize: the client area changes, the swapchain follows, presenting still reads back exact.
    gfx_window_resize(1280, 720);
    int cw = 0, ch = 0;
    wsys_client_size(&cw, &ch);
    for (int i = 0; i < 10; i++)
        if (present_and_check(&s, 0, FRAMES + i, &w, &h)) return fail("presents after resize");
    if (w != cw || h != ch) {
        printf("probe-window: readback %dx%d, client area %dx%d\n", w, h, cw, ch);
        return fail("swapchain size after resize");
    }
    printf("probe-window: resized to %dx%d, presents still exact\n", w, h);
    vk_present_capture(0);

    // Vsync: count ticks between two tick edges about 5 s apart.
    static volatile int32_t ticks;
    gfx_vsync_bind_counter(&ticks);
    long a = ticks;
    while (ticks == a) plat_sleep_ms(1);
    uint64_t t0 = plat_now_ns();
    long n0 = ticks;
    plat_sleep_ms(5000);
    long b = ticks;
    while (ticks == b) plat_sleep_ms(1);
    uint64_t t1 = plat_now_ns();
    long n1 = ticks;
    double rate = (double)(n1 - n0) * 1e9 / (double)(t1 - t0);
    printf("probe-window: vsync %ld ticks in %.3f s = %.3f Hz\n", n1 - n0, (double)(t1 - t0) / 1e9, rate);
    if (rate < 60.0 * 0.995 || rate > 60.0 * 1.005) return fail("vsync rate");

    source_destroy(&s);
    vk_shutdown();
    long errors = vk_validation_errors_total();
    if (errors) {
        printf("probe-window: FAILED (%ld validation errors)\n", errors);
        return 1;
    }
    printf("probe-window: passed (%d frames, %ld validation errors, vsync %.1f Hz)\n", FRAMES, errors, rate);
    return 0;
}

// Original display-encoded texels, independent of game content. Non-endpoint greys make
// an unintended sRGB decode conspicuous (128 becomes about 55); asymmetric RGB catches
// accidental RGBA/BGRA swaps. Upload bytes directly: an sRGB clear would encode floats.
static const uint8_t present_palette[16][4] = {
    {16,16,16,255}, {32,32,32,255}, {64,64,64,255}, {96,96,96,255},
    {128,128,128,255}, {160,160,160,255}, {192,192,192,255}, {235,235,235,255},
    {224,32,64,255}, {48,208,80,255}, {64,96,240,255}, {208,176,32,255},
    {176,48,208,255}, {32,192,208,255}, {24,112,184,255}, {216,144,72,255}
};

static const uint8_t *present_texel(const Source *s, int x, int y, int phase, int solid) {
    if (x < 0) x = 0;
    if (y < 0) y = 0;
    if (x >= s->width) x = s->width - 1;
    if (y >= s->height) y = s->height - 1;
    int tile = solid ? 0 : x / (s->width / 4) + 4 * (y / (s->height / 4));
    return present_palette[(tile + phase) % 16];
}

static int present_upload(Source *s, int phase, int solid, VkImageLayout layout) {
    VkCtx *c = vk_ctx();
    VkDeviceSize bytes = (VkDeviceSize)s->width * s->height * 4;
    if (!s->upload) {
        VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = bytes,
                                 .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT};
        VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_HOST,
            .flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT};
        VmaAllocationInfo info;
        if (vmaCreateBuffer(c->vma, &bi, &ai, &s->upload, &s->upload_alloc, &info) != VK_SUCCESS) return 0;
        s->upload_ptr = info.pMappedData;
    }
    int bgra = s->format == VK_FORMAT_B8G8R8A8_UNORM || s->format == VK_FORMAT_B8G8R8A8_SRGB;
    for (int y = 0; y < s->height; y++) for (int x = 0; x < s->width; x++) {
        const uint8_t *rgba = present_texel(s, x, y, phase, solid);
        uint8_t *p = s->upload_ptr + ((size_t)y * s->width + x) * 4;
        p[0] = rgba[bgra ? 2 : 0]; p[1] = rgba[1]; p[2] = rgba[bgra ? 0 : 2]; p[3] = rgba[3];
    }
    vmaFlushAllocation(c->vma, s->upload_alloc, 0, bytes);
    vkResetCommandBuffer(s->cb, 0);
    VkCommandBufferBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                                     .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    if (vkBeginCommandBuffer(s->cb, &begin) != VK_SUCCESS) return 0;
    barrier(s->cb, s->img, s->layout, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
            s->layout ? VK_ACCESS_2_MEMORY_READ_BIT | VK_ACCESS_2_MEMORY_WRITE_BIT : 0,
            VK_PIPELINE_STAGE_2_TRANSFER_BIT, VK_ACCESS_2_TRANSFER_WRITE_BIT);
    VkBufferImageCopy region = {.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
                                .imageExtent = {(uint32_t)s->width, (uint32_t)s->height, 1}};
    vkCmdCopyBufferToImage(s->cb, s->upload, s->img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);
    barrier(s->cb, s->img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, layout, VK_PIPELINE_STAGE_2_TRANSFER_BIT,
            VK_ACCESS_2_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT, VK_ACCESS_2_MEMORY_READ_BIT);
    if (vkEndCommandBuffer(s->cb) != VK_SUCCESS) return 0;
    VkCommandBufferSubmitInfo ci = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_SUBMIT_INFO, .commandBuffer = s->cb};
    VkSubmitInfo2 si = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO_2, .commandBufferInfoCount = 1, .pCommandBufferInfos = &ci};
    vk_queue_lock();
    VkResult result = vkQueueSubmit2(c->gfx_queue, 1, &si, s->fence);
    vk_queue_unlock();
    if (result != VK_SUCCESS || vkWaitForFences(c->device, 1, &s->fence, VK_TRUE, 5000000000ull) != VK_SUCCESS) return 0;
    vkResetFences(c->device, 1, &s->fence);
    s->layout = layout;
    return 1;
}

static int present_color_check(Source *s, int phase, int solid, const int crop[4], int brightness, int serial, uint64_t *checked) {
    vk_present_image(s->img, s->layout, s->format, s->width, s->height, crop);
    const uint8_t *px;
    int w, h;
    VkFormat format;
    if (!vk_present_captured(&px, &w, &h, &format)) {
        printf("probe-present-color: frame %d has no swapchain readback\n", serial); return 0;
    }
    int bgra = format == VK_FORMAT_B8G8R8A8_UNORM;
    if (!bgra && format != VK_FORMAT_R8G8B8A8_UNORM) {
        printf("probe-present-color: unsupported swapchain format %d\n", (int)format); return 0;
    }
    int cx = crop ? crop[0] : 0, cy = crop ? crop[1] : 0;
    int cw = crop ? crop[2] : s->width, ch = crop ? crop[3] : s->height;
    int rect[4];
    gfx_fit_rect(cw, ch, w, h, rect);
    uint64_t count = 0;
    for (int y = 0; y < h; y++) for (int x = 0; x < w; x++) {
        const uint8_t black[4] = {0,0,0,255};
        const uint8_t *want = black;
        int inside = 0;
        if (x >= rect[0] && y >= rect[1] && x < rect[0] + rect[2] && y < rect[1] + rect[3]) {
            inside = 1;
            // Only compare filter footprints entirely inside one constant tile. Vulkan permits
            // implementation-dependent blit-coordinate rounding and optional crop-edge clamping.
            // A two-source-pixel margin also excludes those boundaries; most pixels remain checked.
            double sx = cx + ((double)(x - rect[0]) + 0.5) * cw / rect[2];
            double sy = cy + ((double)(y - rect[1]) + 0.5) * ch / rect[3];
            if (sx < cx + 2 || sy < cy + 2 || sx >= cx + cw - 2 || sy >= cy + ch - 2) continue;
            want = present_texel(s, (int)sx, (int)sy, phase, solid);
            if (memcmp(want, present_texel(s, (int)sx - 2, (int)sy - 2, phase, solid), 4) ||
                memcmp(want, present_texel(s, (int)sx + 2, (int)sy + 2, phase, solid), 4)) continue;
        }
        const uint8_t *got = px + ((size_t)y * w + x) * 4;
        for (int k = 0; k < 4; k++) {
            int channel = bgra && k != 1 && k != 3 ? 2 - k : k;
            int expected = want[k];
            if (inside && k < 3 && brightness) {
                expected = (int)((want[k] / 255.0f + brightness / 100.0f) * 255.0f + 0.5f);
                if (expected > 255) expected = 255;
            }
            if (abs((int)got[channel] - expected) > 1) {
                printf("probe-present-color: frame %d source=%d layout=%d pixel=%d,%d channel=%d got=%u expected=%u\n",
                       serial, (int)s->format, (int)s->layout, x, y, k, got[channel], expected);
                return 0;
            }
        }
        count++;
    }
    if (count < (uint64_t)w * h / 2) {
        printf("probe-present-color: inadequate checked area (%llu/%llu)\n", (unsigned long long)count,
               (unsigned long long)w * h); return 0;
    }
    *checked += count;
    return 1;
}

static void present_test_setting(const char *name, const char *value) {
    char alias[96];
    snprintf(alias, sizeof alias, "HWAOI_%s", name + 5);
    if (!plat_setenv(alias, value) || !plat_setenv(name, value)) rt_fatal("probe-present-color: test setting %s failed", name);
    cfg_set(name, value);   // invalidate parsed live settings, without writing an ini file
}

int probe_present_color(void) {
    // A deterministic test process only; no game loading or persistent settings writes.
    static const char *keys[] = {"TOTK_COLOR_BRIGHTNESS", "TOTK_COLOR_CONTRAST", "TOTK_COLOR_SATURATION",
                                 "TOTK_COLOR_GAMMA", "TOTK_UPSCALER", "TOTK_ASPECT", "TOTK_FULLSCREEN", "TOTK_PERF_OVERLAY"};
    static const char *values[] = {"0", "100", "100", "1", "bilinear", "fit", "0", "off"};
    for (unsigned i = 0; i < sizeof keys / sizeof keys[0]; i++) present_test_setting(keys[i], values[i]);
    rt_trace |= RT_TRACE_PERF;   // exercise the presentation query pool and its device lifetime
    gfx_window_start();
    if (!vk_ctx()) { puts("probe-present-color: FAILED (no Vulkan device)"); return 1; }
    if (menu_is_open()) menu_toggle();
    vk_present_capture(1);
    const VkFormat formats[] = {VK_FORMAT_R8G8B8A8_UNORM, VK_FORMAT_R8G8B8A8_SRGB,
                                 VK_FORMAT_B8G8R8A8_UNORM, VK_FORMAT_B8G8R8A8_SRGB};
    uint64_t checked = 0;
    int serial = 0, ok = 1;
    for (int shape = 0; shape < 2 && ok; shape++) {
        gfx_window_resize(shape ? 800 : 640, shape ? 600 : 360);
        for (int general = 0; general < 2 && ok; general++) for (int f = 0; f < 4 && ok; f++) {
            Source s = {0};
            int sw = shape ? 160 : 128, sh = shape ? 128 : 96;
            int crop[4] = {17, 13, 96, 64};
            ok = source_create_format(&s, formats[f], sw, sh);
            // Three frames per format visit and reuse both presentation slots, then switch
            // channel order, source layout and dimensions to exercise replacement and barriers.
            for (int phase = 0; phase < 3 && ok; phase++) {
                VkImageLayout layout = general ? VK_IMAGE_LAYOUT_GENERAL : VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
                ok = present_upload(&s, phase * 3, 0, layout) &&
                     present_color_check(&s, phase * 3, 0, shape ? crop : NULL, 0, serial++, &checked);
            }
            source_destroy(&s);
        }
    }
    // The same encoded source is also sampled by the optional FSR pass. Constant fields
    // have an independent exact expectation, unaffected by sharpening or filter details.
    if (ok) {
        present_test_setting("TOTK_UPSCALER", "fsr");
        present_test_setting("TOTK_SHARPNESS", "0");
        for (int f = 0; f < 4 && ok; f++) {
            Source s = {0};
            ok = source_create_format(&s, formats[f], 128, 96);
            for (int phase = 4; phase < 7 && ok; phase++)
                ok = present_upload(&s, phase, 1, VK_IMAGE_LAYOUT_GENERAL) &&
                     present_color_check(&s, phase, 1, NULL, 0, serial++, &checked);
            source_destroy(&s);
        }
    }
    // A non-neutral adjustment must consume encoded channel values as the UNORM control
    // does. Brightness alone has a simple independent expectation: add 0.10, then clamp.
    if (ok) {
        present_test_setting("TOTK_UPSCALER", "bilinear");
        present_test_setting("TOTK_COLOR_BRIGHTNESS", "10");
        for (int f = 0; f < 4 && ok; f++) {
            Source s = {0};
            ok = source_create_format(&s, formats[f], 160, 128);
            int crop[4] = {17, 13, 96, 64};
            for (int phase = 8; phase < 11 && ok; phase++)
                ok = present_upload(&s, phase, 1, VK_IMAGE_LAYOUT_GENERAL) &&
                     present_color_check(&s, phase, 1, crop, 10, serial++, &checked);
            source_destroy(&s);
        }
    }
    vk_present_capture(0);
    vk_shutdown();
    // Recreate the device, then reuse every presentation slot. Stale timestamp
    // handles must not survive shutdown, and the new pool must also be released.
    if (ok) {
        gfx_window_start();
        vk_present_capture(1);
        present_test_setting("TOTK_COLOR_BRIGHTNESS", "0");
        Source s = {0};
        ok = source_create_format(&s, VK_FORMAT_R8G8B8A8_UNORM, 128, 96);
        for (int phase = 0; phase < 3 && ok; phase++)
            ok = present_upload(&s, phase, 1, VK_IMAGE_LAYOUT_GENERAL) &&
                 present_color_check(&s, phase, 1, NULL, 0, serial++, &checked);
        source_destroy(&s);
        vk_present_capture(0);
        vk_shutdown();
    }
    long errors = vk_validation_errors_total();
    printf("probe-present-color: %s (%d frames, %llu checked pixels, %ld validation errors; RGBA/BGRA UNORM/sRGB, crop, resize, slots, FSR, colour adjustment, device recreation)\n",
           ok && !errors ? "passed" : "FAILED", serial, (unsigned long long)checked, errors);
    return ok && !errors ? 0 : 1;
}
