// Original, bounded GSV transport subset. Protocol observations are described in
// docs/hwaoi/gsv-bootstrap.md. No native GPU memory, camera or network data path
// is advertised or silently accepted by this adapter.
#include <stdlib.h>
#include <string.h>
#include "gsv.h"
#include "platform/platform.h"

typedef struct {
    PlatMutex lock;
    uint64_t aruid, boot_version, remote_usage;
    uint32_t process;
    uint32_t graphics_frame_policy;
    int graphics_frame_policy_set;
    int booted, registered, context, chat_context, intermodule_created, intermodule_ready, display_context;
} GsvUser;

typedef struct {
    PlatMutex lock;
    uint32_t next;
    uint32_t event[1024];
    uint64_t display_key[1024];
    uint8_t display_open[1024];
} GsvChat;

typedef struct {
    PlatMutex lock;
    uint64_t version;
    uint32_t notifications[1024], slot_events[8];
    uint64_t display_names[8];
    uint64_t active_layers[8], layer_selectors[8];
    uint8_t display_open[8];
    struct { uint64_t client_tag; uint32_t peer, bound_display; uint8_t open, role, buffer_owned[15]; } layers[1024];
} GsvDisplay;

static void display_close(void *p) {
    GsvDisplay *display = p;
    for (unsigned i = 0; i < 1024; i++) if (display->notifications[i]) kern_handle_close(display->notifications[i]);
    for (unsigned i = 0; i < 8; i++) if (display->slot_events[i]) kern_handle_close(display->slot_events[i]);
    free(display);
}

static int plain(const IpcRequest *q) {
    return !(q->nx || q->na || q->nb || q->nw || q->nc || q->nmove);
}

static void display_request(IpcSession *s, const IpcRequest *q, IpcResponse *r) {
    GsvDisplay *display = s->state;
    if (q->type < 16 || !plain(q) || q->ncopy) ipc_unimplemented(s, q);
    switch (q->cmd) {
    case 0: { uint64_t token = IPC_ARG(q, uint64_t, 0); IPC_OUT(r, token); return; }
    // These are local pool bounds, not physical monitor counts. The pinned SDK
    // uses (bound+1) slots and rounds the notification bound into a bitset.
    case 1: { uint32_t bound = 1023; IPC_OUT(r, bound); return; }
    case 3: { uint32_t bound = 7; IPC_OUT(r, bound); return; }
    case 11: {
        uint32_t id = IPC_ARG(q, uint32_t, 0);
        if (id >= 1024) ipc_unimplemented(s, q);
        if (!display->notifications[id]) display->notifications[id] = kern_event_new();
        r->copy[r->ncopy++] = kern_event_readable(display->notifications[id]);
        return;
    }
    case 31: {
        uint32_t id = IPC_ARG(q, uint32_t, 0);
        if (id >= 8) ipc_unimplemented(s, q);
        if (!display->slot_events[id]) display->slot_events[id] = kern_event_new();
        r->copy[r->ncopy++] = kern_event_readable(display->slot_events[id]);
        return;
    }
    case 101: { // Target VI2 client: u64 display name + client-allocated display slot
        uint64_t name = IPC_ARG(q, uint64_t, 0);
        uint32_t slot = IPC_ARG(q, uint32_t, 8);
        if (slot >= 8 || display->display_open[slot]) ipc_unimplemented(s, q);
        display->display_names[slot] = name;
        display->display_open[slot] = 1;
        rt_log("gsv display: local slot %u opened, name token %#llx", slot, (unsigned long long)name);
        return;
    }
    case 201: { // Target VI2 client: opaque client object tag + two allocated endpoint slots
        uint64_t tag = IPC_ARG(q, uint64_t, 0);
        uint32_t a = IPC_ARG(q, uint32_t, 8), b = IPC_ARG(q, uint32_t, 12);
        if (a >= 1024 || b >= 1024 || a == b || display->layers[a].open || display->layers[b].open)
            ipc_unimplemented(s, q);
        display->layers[a].client_tag = display->layers[b].client_tag = tag;
        display->layers[a].peer = b;
        display->layers[b].peer = a;
        display->layers[a].role = 0;
        display->layers[b].role = 1;
        display->layers[a].open = display->layers[b].open = 1;
        rt_log("gsv display: local layer endpoint pair %u/%u created, client tag %#llx", a, b, (unsigned long long)tag);
        return;
    }
    case 212: { // Query local queue-slot availability, not GPU image validity
        uint64_t tag = IPC_ARG(q, uint64_t, 0), available = 0;
        unsigned layer = 1024;
        for (unsigned i = 0; i < 1024; i++)
            if (display->layers[i].open && !display->layers[i].role && display->layers[i].client_tag == tag) {
                layer = i;
                break;
            }
        if (layer == 1024) ipc_unimplemented(s, q);
        // The target SDK rejects slot >=15 (sdk+0x5652d0/0x566fc4).
        // Newly created local queues have no producer/consumer-owned slots.
        // Image submission/queue transitions are not yet accepted by this adapter.
        for (unsigned i = 0; i < 15; i++) if (!display->layers[layer].buffer_owned[i]) available |= UINT64_C(1) << i;
        IPC_OUT(r, available);
        return;
    }
    case 251: { // SDK layer attach: opaque layer tag, then the display's name token
        uint64_t tag = IPC_ARG(q, uint64_t, 0), name = IPC_ARG(q, uint64_t, 8);
        unsigned layer = 1024, target = 8;
        for (unsigned i = 0; i < 1024; i++)
            if (display->layers[i].open && !display->layers[i].role && display->layers[i].client_tag == tag) layer = i;
        for (unsigned i = 0; i < 8; i++)
            if (display->display_open[i] && display->display_names[i] == name) target = i;
        if (layer == 1024 || target == 8 || display->layers[layer].bound_display) ipc_unimplemented(s, q);
        display->layers[layer].bound_display = target + 1;
        display->layers[display->layers[layer].peer].bound_display = target + 1;
        rt_log("gsv display: layer pair %u/%u attached to local display %u", layer, display->layers[layer].peer, target);
        return;
    }
    case 111: { // SDK active-list selection: display name, layer-group name, selected layer tag
        uint64_t name = IPC_ARG(q, uint64_t, 0), selector = IPC_ARG(q, uint64_t, 8), tag = IPC_ARG(q, uint64_t, 16);
        unsigned target = 8, layer = 1024;
        for (unsigned i = 0; i < 8; i++) if (display->display_open[i] && display->display_names[i] == name) target = i;
        for (unsigned i = 0; i < 1024; i++)
            if (display->layers[i].open && !display->layers[i].role && display->layers[i].client_tag == tag) layer = i;
        rt_log("gsv display: selecting layer tag %#llx for name %#llx, selector %#llx",
               (unsigned long long)tag, (unsigned long long)name, (unsigned long long)selector);
        // Target StreamPlay uses the ASCII name "default" for this layer group.
        if (target == 8 || layer == 1024 || display->layers[layer].bound_display != target + 1 ||
            selector != UINT64_C(0x00746c7561666564))
            ipc_unimplemented(s, q);
        display->active_layers[target] = tag;
        display->layer_selectors[target] = selector;
        return;
    }
    default: ipc_unimplemented(s, q);
    }
}

static void display_handler(IpcSession *s, const IpcRequest *q, IpcResponse *r) {
    GsvDisplay *display = s->state;
    plat_mutex_lock(&display->lock);
    display_request(s, q, r);
    plat_mutex_unlock(&display->lock);
}

static void chat_close(void *p) {
    GsvChat *chat = p;
    for (unsigned i = 0; i < chat->next; i++) if (chat->event[i]) kern_handle_close(chat->event[i]);
    free(chat);
}

static void chat_request(IpcSession *s, const IpcRequest *q, IpcResponse *r) {
    GsvChat *chat = s->state;
    if (q->type < 16 || !plain(q) || q->ncopy) ipc_unimplemented(s, q);
    switch (q->cmd) {
    case 1: {
        if (chat->next == 1024) rt_fatal("gsv chat: event subscription limit");
        uint32_t id = chat->next++;
        chat->event[id] = kern_event_new();
        IPC_OUT(r, id);
        rt_log("gsv chat: allocated inactive event subscription %u", id);
        return;
    }
    case 11: {
        uint32_t id = IPC_ARG(q, uint32_t, 0);
        if (id >= chat->next) rt_fatal("gsv chat: invalid subscription %u", id);
        r->copy[r->ncopy++] = kern_event_readable(chat->event[id]);
        return;
    }
    case 103: { // SDK OpenVirtualDisplay: u64 name key + client-allocated u32 slot
        uint64_t key = IPC_ARG(q, uint64_t, 0);
        uint32_t slot = IPC_ARG(q, uint32_t, 8);
        if (slot >= 1024 || chat->display_open[slot]) ipc_unimplemented(s, q);
        chat->display_key[slot] = key;
        chat->display_open[slot] = 1;
        rt_log("gsv chat: local virtual-display slot %u registered (no image producer)", slot);
        return;
    }
    default: ipc_unimplemented(s, q);
    }
}

static void chat_handler(IpcSession *s, const IpcRequest *q, IpcResponse *r) {
    GsvChat *chat = s->state;
    plat_mutex_lock(&chat->lock);
    chat_request(s, q, r);
    plat_mutex_unlock(&chat->lock);
}

static void user_request(IpcSession *s, const IpcRequest *q, IpcResponse *r) {
    GsvUser *state = s->state;
    if (q->type < 16 || !plain(q)) ipc_unimplemented(s, q);
    if (q->cmd != 1111 || (rt_trace & RT_TRACE_IPC))
        rt_log("gsv: TIPC %u (data %u, copy %u)", q->cmd, q->data_size, q->ncopy);
    switch (q->cmd) {
    case 4: { // BootWithRemoteMemory: process handle, aruid + 5 u64 fields
        if (state->booted || q->ncopy != 1) ipc_unimplemented(s, q);
        const uint64_t *fields = ipc_arg(q, 0, 48);
        uint64_t value[6];
        memcpy(value, fields, sizeof value);
        if (value[1] != UINT64_C(0x8000000000000005) || value[2] || value[3] || value[4])
            ipc_unimplemented(s, q);
        state->process = q->copy[0];
        state->aruid = value[0];
        state->boot_version = value[1];
        state->remote_usage = value[5];
        state->booted = 1;
        rt_log("gsv: local process context booted, aruid=%llu, no remote GPU allocation",
               (unsigned long long)state->aruid);
        return;
    }
    case 1111: { // SetGraphicsFrameLengthMode: explicit experimental host-only record
        uint32_t wire = IPC_ARG(q, uint32_t, 0);
        int report = (rt_trace & RT_TRACE_IPC) || !state->graphics_frame_policy_set || state->graphics_frame_policy != wire;
        if (report) rt_log("gsv: GraphicsFrameLengthMode request wire=%#x (%u), context=%d",
                           wire, wire, state->context);
        // SDK 46177610... sdk+0x411660 maps enum 0..3 through +0xae5660
        // to these opaque words. Client code establishes no duration unit or
        // server-side scheduling semantics. Never interpret them as FPS/ns.
        const char *local_policy = getenv("HWAOI_GSV_LOCAL_FRAME_POLICY");
        if (!local_policy || strcmp(local_policy, "1")) {
            rt_log("gsv: GraphicsFrameLengthMode unsupported by default; console scheduling semantics unverified");
            ipc_unimplemented(s, q);
        }
        if (!state->booted || !state->registered || !state->context || q->ncopy || q->has_pid ||
            q->data_size != 4 || (wire != 0 && wire != 0x11e && wire != 0x13c && wire != 0x178))
            ipc_unimplemented(s, q);
        state->graphics_frame_policy = wire;
        state->graphics_frame_policy_set = 1;
        if (report) rt_log("gsv: EXPERIMENTAL local frame-policy request %#x recorded only; console GPU scheduling is NOT "
                           "emulated; NVN swap interval still controls host pacing", wire);
        return;
    }
    case 11: // SetCurrentProcessHandle
        if (!state->booted || state->registered || q->ncopy != 1 || q->copy[0] != state->process)
            ipc_unimplemented(s, q);
        state->registered = 1;
        return;
    case 101: // CreateContext: a local transport context, not a GPU channel
        if (!state->registered || state->context || q->ncopy) ipc_unimplemented(s, q);
        state->context = 1;
        return;
    case 1501: // CreateInterModuleImageContext: no resource or handle output
        if (!state->context || state->intermodule_created || q->ncopy) ipc_unimplemented(s, q);
        state->intermodule_created = 1;
        return;
    case 1502: // InitializeInterModuleImageContext: record local lifecycle only
        if (!state->intermodule_created || state->intermodule_ready || q->ncopy) ipc_unimplemented(s, q);
        state->intermodule_ready = 1;
        return;
    case 301: // CreateDisplayContext: allocate the local interface namespace
        if (!state->context || state->display_context || q->ncopy) ipc_unimplemented(s, q);
        state->display_context = 1;
        return;
    case 305: { // InitializeDisplayContext(version) -> raw TIPC DisplaySession
        if (!state->display_context || q->ncopy) ipc_unimplemented(s, q);
        uint64_t version = IPC_ARG(q, uint64_t, 0);
        if (version != UINT64_C(0x300020000)) ipc_unimplemented(s, q);
        GsvDisplay *display = calloc(1, sizeof *display);
        if (!display) rt_fatal("gsv display: allocation");
        plat_mutex_init(&display->lock);
        display->version = version;
        r->copy[r->ncopy++] = ipc_session_new("gsv DisplaySession", display_handler, display, display_close);
        rt_log("gsv: display-session version %#llx created; display operations checked separately", (unsigned long long)version);
        return;
    }
    case 1201: // CreateChatContext
        if (!state->context || state->chat_context || q->ncopy) ipc_unimplemented(s, q);
        state->chat_context = 1;
        return;
    case 1202: { // InitializeChatContext: copied raw TIPC session, not a CMIF object ID
        if (!state->chat_context || q->ncopy) ipc_unimplemented(s, q);
        GsvChat *chat = calloc(1, sizeof *chat);
        if (!chat) rt_fatal("gsv chat: allocation");
        plat_mutex_init(&chat->lock);
        r->copy[r->ncopy++] = ipc_session_new("gsv ChatSession", chat_handler, chat, chat_close);
        return;
    }
    default: ipc_unimplemented(s, q);
    }
}

static void user_handler(IpcSession *s, const IpcRequest *q, IpcResponse *r) {
    GsvUser *state = s->state;
    plat_mutex_lock(&state->lock);
    user_request(s, q, r);
    plat_mutex_unlock(&state->lock);
}

uint32_t gsv_user_session_new(void) {
    GsvUser *state = calloc(1, sizeof *state);
    if (!state) rt_fatal("gsv: session allocation");
    plat_mutex_init(&state->lock);
    return ipc_session_new("gsv UserSession", user_handler, state, free);
}

// Result nn::am::GetGraphicsSupervisor(sf::Out<NativeHandle>): the target's Out
// points to {u32 handle, bool managed}. Keep the real SDK session wrapper/Boot
// code, supplying only the raw transport am would have returned.
static void get_supervisor(Cpu *c) {
    uint8_t *out = arg_p(c, 0);
    if (!out) rt_fatal("gsv: missing NativeHandle output");
    uint32_t handle = gsv_user_session_new();
    memset(out, 0, 8);
    memcpy(out, &handle, 4);
    out[4] = 1;
    ret_x(c, 0);
}
HOST_OVERRIDE("_ZN2nn2am21GetGraphicsSupervisorENS_2sf3OutINS1_12NativeHandleEvEE", get_supervisor);
