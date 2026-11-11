/* Original synthetic-only fixture. Compiles the actual IPC and chat adapter.
 * Single-thread kernel mocks test framing and ownership, not OS scheduling. */
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void fixture_check(int ok, int line, const char *what) {
    if (!ok) { fprintf(stderr, "fixture assertion at %d: %s\n", line, what); exit(1); }
}
#define TASSERT(x) fixture_check(!!(x), __LINE__, #x)

#include "../../runtime/ipc.c"
#include "../../runtime/chat.c"
#include "../../runtime/gsv.c"

static jmp_buf fixture_fatal_jump;
static int fixture_catch_fatal;
static char fixture_fatal_text[512];
static int fixture_event_count, fixture_writable_count, fixture_closed;

unsigned rt_trace;
static unsigned gsv_policy_logs, gsv_tipc_logs;
void rt_log(const char *fmt, ...) {
    if (strstr(fmt,"gsv: EXPERIMENTAL local frame-policy")) gsv_policy_logs++;
    if (strstr(fmt,"gsv: TIPC")) gsv_tipc_logs++;
}
void rt_logf(const char *fmt, ...) { (void)fmt; }
void rt_log_raw(const char *text, size_t size) { (void)text; (void)size; }
void rt_fatal(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(fixture_fatal_text, sizeof fixture_fatal_text, fmt, ap);
    va_end(ap);
    if (fixture_catch_fatal) longjmp(fixture_fatal_jump, 1);
    fprintf(stderr, "unexpected fatal: %s\n", fixture_fatal_text);
    exit(70);
}

const char *ldr_describe(uint64_t address, char *buffer, int size) {
    snprintf(buffer, (size_t)size, "fixture:%llx", (unsigned long long)address);
    return buffer;
}
int plat_mem_readable(uint64_t address, uint64_t size) { (void)address; (void)size; return 0; }
void plat_mutex_init(PlatMutex *mutex) { mutex->p = NULL; }
void plat_mutex_lock(PlatMutex *mutex) { TASSERT(!mutex->p); mutex->p = (void *)1; }
void plat_mutex_unlock(PlatMutex *mutex) { TASSERT(mutex->p); mutex->p = NULL; }

/* The included file also contains whole-game/audio probes. They are never
 * exercised here; fail-fast definitions keep accidental coverage impossible. */
#define FIXTURE_UNUSED() rt_fatal("out-of-scope probe dependency: %s", __func__)
const int rt_game_version = 0;
void kern_run_on_guest_main(void (*fn)(Cpu *)) { (void)fn; FIXTURE_UNUSED(); }
const RtConfig *rt_config(void) { FIXTURE_UNUSED(); }
void ldr_load(const char *path) { (void)path; FIXTURE_UNUSED(); }
int plat_run_self(const char *n, const char *v, char *out, size_t cap, uint32_t ms) {
    (void)n; (void)v; (void)out; (void)cap; (void)ms; FIXTURE_UNUSED();
}
RtStats rt_stats(void) { FIXTURE_UNUSED(); }
void rt_svc(Cpu *cpu, uint32_t imm, uint64_t pc) { (void)cpu; (void)imm; (void)pc; FIXTURE_UNUSED(); }
void *mem_kernel_alloc(uint64_t size, uint64_t alignment) { (void)size; (void)alignment; FIXTURE_UNUSED(); }
void mem_kernel_free(void *memory, uint64_t size) { (void)memory; (void)size; FIXTURE_UNUSED(); }
long audren_renderers_live(void) { FIXTURE_UNUSED(); }
uint64_t kern_now_ns(void) { FIXTURE_UNUSED(); }
uint32_t kern_main_thread(Cpu **cpu) { (void)cpu; FIXTURE_UNUSED(); }
const RtLoaded *ldr_module(int index) { (void)index; FIXTURE_UNUSED(); }
uint64_t ldr_plt_stub(const char *module, const char *name) { (void)module; (void)name; FIXTURE_UNUSED(); }
uint64_t rt_call(Cpu *cpu, uint64_t function, int count, ...) { (void)cpu; (void)function; (void)count; FIXTURE_UNUSED(); }
void plat_event_init(PlatEvent *event, int manual, int set) { (void)event; (void)manual; (void)set; FIXTURE_UNUSED(); }
void plat_event_set(PlatEvent *event) { (void)event; FIXTURE_UNUSED(); }
void plat_event_wait(PlatEvent *event) { (void)event; FIXTURE_UNUSED(); }
int plat_event_wait_ns(PlatEvent *event, uint64_t ns) { (void)event; (void)ns; FIXTURE_UNUSED(); }
PlatThread *plat_thread_new(PlatThreadFn fn, void *arg, const char *name) {
    (void)fn; (void)arg; (void)name; FIXTURE_UNUSED();
}
int plat_thread_join_ns(PlatThread *thread, uint64_t ns) { (void)thread; (void)ns; FIXTURE_UNUSED(); }
void audren_manager_handler(IpcSession *s, const IpcRequest *q, IpcResponse *r) {
    (void)s; (void)q; (void)r; FIXTURE_UNUSED();
}
void aud_user_handler(IpcSession *s, const IpcRequest *q, IpcResponse *r) {
    (void)s; (void)q; (void)r; FIXTURE_UNUSED();
}
void hwopus_manager_handler(IpcSession *s, const IpcRequest *q, IpcResponse *r) {
    (void)s; (void)q; (void)r; FIXTURE_UNUSED();
}

enum { FIXTURE_EVENT_W = 100, FIXTURE_EVENT_R = 101, FIXTURE_HANDLES = 8192 };
typedef struct { unsigned references; int signalled; } FixtureEvent;
typedef struct { int kind; void *object; } FixtureHandle;
static FixtureHandle fixture_handles[FIXTURE_HANDLES];

uint32_t kern_handle_new(int kind, void *object) {
    for (uint32_t i = 1; i < FIXTURE_HANDLES; i++)
        if (!fixture_handles[i].object) {
            fixture_handles[i] = (FixtureHandle){kind, object};
            return i;
        }
    rt_fatal("fixture handle exhaustion");
}
IpcSession *kern_session_acquire(uint32_t handle) {
    if (handle >= FIXTURE_HANDLES || fixture_handles[handle].kind != KERN_SESSION || !fixture_handles[handle].object)
        return NULL;
    IpcSession *object = fixture_handles[handle].object;
    ipc_session_retain(object);
    return object;
}
void kern_handle_close(uint32_t handle) {
    TASSERT(handle && handle < FIXTURE_HANDLES && fixture_handles[handle].object);
    FixtureHandle old = fixture_handles[handle];
    fixture_handles[handle] = (FixtureHandle){0};
    if (old.kind == KERN_SESSION) ipc_session_release(old.object);
    else {
        TASSERT(old.kind == FIXTURE_EVENT_W || old.kind == FIXTURE_EVENT_R);
        FixtureEvent *event = old.object;
        TASSERT(event->references > 0);
        if (old.kind == FIXTURE_EVENT_W) fixture_writable_count--;
        if (!--event->references) { fixture_event_count--; free(event); }
    }
}
uint32_t kern_event_new(void) {
    FixtureEvent *event = calloc(1, sizeof *event);
    TASSERT(event != NULL);
    event->references = 1;
    fixture_event_count++;
    fixture_writable_count++;
    return kern_handle_new(FIXTURE_EVENT_W, event);
}
uint32_t kern_event_readable(uint32_t writable) {
    TASSERT(writable < FIXTURE_HANDLES && fixture_handles[writable].kind == FIXTURE_EVENT_W);
    FixtureEvent *event = fixture_handles[writable].object;
    TASSERT(event && !event->signalled);
    event->references++;
    return kern_handle_new(FIXTURE_EVENT_R, event);
}

static void empty_everything(void) {
    TASSERT(ipc_sessions_live() == 0);
    TASSERT(fixture_event_count == 0 && fixture_writable_count == 0);
    for (unsigned i = 1; i < FIXTURE_HANDLES; i++) TASSERT(fixture_handles[i].object == NULL);
}

/* Independent literal wire construction: HIPC two-word header, 16-byte alignment,
 * then either SFCI or the domain envelope followed by SFCI. */
static void wire_plain(uint32_t *message, unsigned type, unsigned command, const void *data, unsigned size) {
    TASSERT(size <= 64);
    memset(message, 0, IPC_MSG_BYTES);
    message[0] = type;
    message[1] = (32 + size + 3) / 4;
    message[4] = UINT32_C(0x49434653);
    message[6] = command;
    if (size) memcpy((unsigned char *)message + 32, data, size);
}
static void wire_domain(uint32_t *message, unsigned type, uint32_t object, unsigned command,
                        const void *data, unsigned size) {
    TASSERT(size <= 64);
    memset(message, 0, IPC_MSG_BYTES);
    message[0] = 4;
    message[1] = type == 2 ? 8 : (48 + size + 3) / 4;
    unsigned char *header = (unsigned char *)message + 16;
    header[0] = (unsigned char)type;
    uint16_t length = type == 2 ? 0 : (uint16_t)(16 + size);
    memcpy(header + 2, &length, 2);
    memcpy(header + 4, &object, 4);
    if (type != 2) {
        message[8] = UINT32_C(0x49434653);
        message[10] = command;
        if (size) memcpy((unsigned char *)message + 48, data, size);
    }
}
static void wire_tipc(uint32_t *message, unsigned command, const void *data, unsigned size,
                      unsigned copies, uint32_t copy_handle) {
    TASSERT(size <= 64 && copies <= 1);
    memset(message, 0, IPC_MSG_BYTES);
    message[0] = command + 16;
    unsigned word = 2;
    if (copies) { message[word++] = copies << 1; message[word++] = copy_handle; }
    message[1] = (size + 3) / 4 | (copies ? UINT32_C(0x80000000) : 0);
    if (size) memcpy(message + word, data, size);
}
static uint32_t tipc_success(uint32_t *message, unsigned copies) {
    unsigned word = 2, actual_copies = 0;
    if (message[1] >> 31) {
        unsigned special = message[word++];
        TASSERT(!(special & 1) && ((special >> 5) & 15) == 0);
        actual_copies = (special >> 1) & 15;
        word += actual_copies;
    }
    TASSERT(actual_copies == copies && message[word] == 0);
    return copies ? message[3] : message[word + 1];
}
static unsigned char *reply_payload(uint32_t *message, unsigned *copies, unsigned *moves) {
    unsigned word = 2;
    *copies = *moves = 0;
    if (message[1] & UINT32_C(0x80000000)) {
        unsigned special = message[word++];
        TASSERT(!(special & 1));
        *copies = (special >> 1) & 15;
        *moves = (special >> 5) & 15;
        word += *copies + *moves;
    }
    word += 2 * ((message[0] >> 16) & 15);
    return (unsigned char *)message + ((word * 4 + 15) & ~15u);
}
static void plain_success(uint32_t *message, unsigned copies, unsigned moves) {
    unsigned actual_copies, actual_moves;
    uint32_t *payload = (uint32_t *)reply_payload(message, &actual_copies, &actual_moves);
    TASSERT(actual_copies == copies && actual_moves == moves);
    TASSERT(payload[0] == UINT32_C(0x4f434653) && payload[2] == 0);
}
static uint32_t domain_result(uint32_t *message, unsigned copies, unsigned objects) {
    unsigned actual_copies, moves;
    uint32_t *payload = (uint32_t *)reply_payload(message, &actual_copies, &moves);
    TASSERT(actual_copies == copies && moves == 0);
    TASSERT(payload[0] == objects && payload[1] == 0 && payload[2] == 0 && payload[3] == 0);
    TASSERT(payload[4] == UINT32_C(0x4f434653) && payload[6] == 0);
    return objects ? payload[8] : 0;
}
static uint32_t convert_wire(IpcSession *session) {
    uint32_t message[IPC_MSG_BYTES / 4];
    wire_plain(message, 5, 0, NULL, 0);
    dispatch(session, message);
    plain_success(message, 0, 0);
    TASSERT(message[8] != 0 && session->domain != NULL);
    return message[8];
}
static uint32_t control_handle(IpcSession *session, unsigned command, uint32_t value) {
    uint32_t message[IPC_MSG_BYTES / 4];
    wire_plain(message, 5, command, &value, command == 2 ? 0 : 4);
    dispatch(session, message);
    plain_success(message, 0, 1);
    return message[3];
}

static void case_lifecycle(void) {
    uint32_t handle = ipc_session_new("fixture chat", chat_user_handler, NULL, NULL);
    IpcSession *root = kern_session_acquire(handle);
    uint32_t id = convert_wire(root);
    uint32_t message[IPC_MSG_BYTES / 4];
    wire_domain(message, 1, id, 0, NULL, 0);
    dispatch(root, message);
    uint32_t child_id = domain_result(message, 0, 1);
    TASSERT(child_id != id);
    TASSERT(fixture_event_count == 1 && fixture_writable_count == 1);
    wire_domain(message, 1, child_id, 1, NULL, 0);
    dispatch(root, message);
    domain_result(message, 1, 0);
    uint32_t event_a = message[3];
    wire_domain(message, 1, child_id, 1, NULL, 0);
    dispatch(root, message);
    domain_result(message, 1, 0);
    uint32_t event_b = message[3];
    TASSERT(event_a != event_b && fixture_handles[event_a].object == fixture_handles[event_b].object);
    TASSERT(((FixtureEvent *)fixture_handles[event_a].object)->references == 3);

    uint32_t mode = UINT32_C(0x12345678);
    wire_domain(message, 1, id, 23, &mode, sizeof mode);
    dispatch(root, message);
    domain_result(message, 0, 0);
    TASSERT(chat_requested_sharing_mode() == mode);

    uint32_t clone = control_handle(root, 2, 0);
    IpcSession *cloned = kern_session_acquire(clone);
    TASSERT(cloned == root && cloned->domain == root->domain);
    ipc_session_release(cloned);
    uint32_t plain_handle = control_handle(root, 1, child_id);
    IpcSession *plain = kern_session_acquire(plain_handle);
    TASSERT(plain->domain == NULL);
    wire_domain(message, 2, child_id, 0, NULL, 0);
    dispatch(root, message);
    TASSERT(root->domain->objects[child_id] == NULL);
    TASSERT(fixture_writable_count == 1); // copied ordinary session still owns the scoped service
    wire_plain(message, 4, 1, NULL, 0);
    dispatch(plain, message);
    plain_success(message, 1, 0);
    uint32_t event_c = message[3];
    TASSERT(fixture_handles[event_c].object == fixture_handles[event_a].object);
    ipc_session_release(plain);
    kern_handle_close(plain_handle);
    TASSERT(fixture_writable_count == 0 && fixture_event_count == 1);
    kern_handle_close(handle);
    ipc_session_release(root);
    kern_handle_close(clone);
    TASSERT(ipc_sessions_live() == 0);
    kern_handle_close(event_a);
    kern_handle_close(event_b);
    kern_handle_close(event_c);
    empty_everything();
}

static void state_close(void *state) { TASSERT(state == &fixture_closed); fixture_closed++; }
static void state_handler(IpcSession *session, const IpcRequest *request, IpcResponse *response) {
    TASSERT(session->state == &fixture_closed && request->cmd == 7);
    uint32_t value = UINT32_C(0xaabbccdd);
    IPC_OUT(response, value);
}
static void case_copy_root(void) {
    uint32_t handle = ipc_session_new("fixture state", state_handler, &fixture_closed, state_close);
    IpcSession *root = kern_session_acquire(handle);
    uint32_t id = convert_wire(root);
    uint32_t copy_handle = control_handle(root, 1, id);
    IpcSession *copy = kern_session_acquire(copy_handle);
    TASSERT(copy != root && !copy->domain && copy->state == root->state);
    domain_close(root->domain, id);
    kern_handle_close(handle);
    ipc_session_release(root);
    TASSERT(fixture_closed == 0);
    uint32_t message[64];
    wire_plain(message, 4, 7, NULL, 0);
    dispatch(copy, message);
    plain_success(message, 0, 0);
    TASSERT(message[8] == UINT32_C(0xaabbccdd));
    ipc_session_release(copy);
    kern_handle_close(copy_handle);
    TASSERT(fixture_closed == 1);
    empty_everything();
}

static void case_native_move(void) {
    uint32_t handle = ipc_session_new("fixture chat", chat_user_handler, NULL, NULL);
    IpcSession *root = kern_session_acquire(handle);
    convert_wire(root);
    uint32_t writable = kern_event_new();
    IpcResponse response = {0};
    response.move[response.nmove++] = kern_event_readable(writable);
    response.move[response.nmove++] = ipc_session_new("fixture nested", chat_user_handler, NULL, NULL);
    domain_outputs(root->domain, &response);
    TASSERT(response.nmove == 1 && response.ndomain == 1);
    TASSERT(fixture_handles[response.move[0]].kind == FIXTURE_EVENT_R);
    kern_handle_close(response.move[0]);
    kern_handle_close(writable);
    domain_close(root->domain, response.domain_objects[0]);
    ipc_session_release(root);
    kern_handle_close(handle);
    empty_everything();
}

static void case_copy_conversion_isolation(void) {
    uint32_t handle = ipc_session_new("fixture state", state_handler, &fixture_closed, state_close);
    IpcSession *root = kern_session_acquire(handle);
    uint32_t original_id = convert_wire(root);
    uint32_t first_handle = control_handle(root, 1, original_id);
    uint32_t second_handle = control_handle(root, 1, original_id);
    IpcSession *first = kern_session_acquire(first_handle);
    IpcSession *second = kern_session_acquire(second_handle);
    TASSERT(first != second && first != root && second != root);
    uint32_t first_id = convert_wire(first);
    TASSERT(first->domain != root->domain && second->domain == NULL);
    TASSERT(root->domain->objects[original_id]->domain == NULL);
    uint32_t message[64];
    wire_plain(message, 4, 7, NULL, 0);
    dispatch(second, message);
    plain_success(message, 0, 0);
    TASSERT(message[8] == UINT32_C(0xaabbccdd));
    wire_domain(message, 1, first_id, 7, NULL, 0);
    dispatch(first, message);
    domain_result(message, 0, 0);
    TASSERT(message[12] == UINT32_C(0xaabbccdd));
    ipc_session_release(root);
    kern_handle_close(handle);
    TASSERT(fixture_closed == 0);
    ipc_session_release(first);
    kern_handle_close(first_handle);
    TASSERT(fixture_closed == 0);
    ipc_session_release(second);
    kern_handle_close(second_handle);
    TASSERT(fixture_closed == 1);
    empty_everything();
}

static void case_plain_clone_conversion_isolation(void) {
    uint32_t handle = ipc_session_new("fixture state", state_handler, &fixture_closed, state_close);
    IpcSession *root = kern_session_acquire(handle);
    uint32_t clone_handle = control_handle(root, 2, 0);
    IpcSession *clone = kern_session_acquire(clone_handle);
    TASSERT(clone != root && !clone->domain && !root->domain);
    convert_wire(clone);
    TASSERT(!root->domain && clone->domain);
    uint32_t message[64];
    wire_plain(message, 4, 7, NULL, 0);
    dispatch(root, message);
    plain_success(message, 0, 0);
    TASSERT(message[8] == UINT32_C(0xaabbccdd));
    ipc_session_release(root);
    kern_handle_close(handle);
    TASSERT(fixture_closed == 0);
    ipc_session_release(clone);
    kern_handle_close(clone_handle);
    TASSERT(fixture_closed == 1);
    empty_everything();
}

static void case_output_object_offset(void) {
    uint32_t message[64];
    IpcResponse response = {0};
    response.data[0] = 0x7f;
    response.data_size = 1;
    response.domain_objects[0] = UINT32_C(0x12345678);
    response.ndomain = 1;
    respond(message, 0, 1, &response);
    unsigned copies, moves;
    unsigned char *payload = reply_payload(message, &copies, &moves);
    TASSERT(copies == 0 && moves == 0 && payload[32] == 0x7f);
    uint32_t object;
    memcpy(&object, payload + 33, sizeof object);
    TASSERT(object == UINT32_C(0x12345678)); // no gratuitous alignment between data and object IDs
    empty_everything();
}

static void case_clone_ex(void) {
    uint32_t handle = ipc_session_new("fixture chat", chat_user_handler, NULL, NULL);
    IpcSession *root = kern_session_acquire(handle);
    uint32_t id = convert_wire(root);
    uint32_t clone_handle = control_handle(root, 4, 0x8888);
    IpcSession *clone = kern_session_acquire(clone_handle);
    TASSERT(clone == root && clone->domain == root->domain);
    IpcSession *object = domain_get(clone->domain, id);
    TASSERT(object == root->domain->objects[id]);
    ipc_session_release(object);
    ipc_session_release(clone);
    ipc_session_release(root);
    kern_handle_close(handle);
    kern_handle_close(clone_handle);
    empty_everything();
}

static void case_invalid_id(void) {
    uint32_t handle = ipc_session_new("fixture chat", chat_user_handler, NULL, NULL);
    IpcSession *root = kern_session_acquire(handle);
    uint32_t id = convert_wire(root);
    domain_close(root->domain, id);
    fixture_catch_fatal = 1;
    if (!setjmp(fixture_fatal_jump)) { (void)domain_get(root->domain, id); TASSERT(0); }
    fixture_catch_fatal = 0;
    TASSERT(strstr(fixture_fatal_text, "closed domain object") != NULL);
    ipc_session_release(root);
    kern_handle_close(handle);
    empty_everything();
}

static void case_unknown_chat(void) {
    IpcSession *root = object_new("fixture chat", chat_user_handler, NULL, NULL);
    IpcRequest request = {.cmd = 999};
    IpcResponse response = {0};
    fixture_catch_fatal = 1;
    if (!setjmp(fixture_fatal_jump)) { chat_user_handler(root, &request, &response); TASSERT(0); }
    fixture_catch_fatal = 0;
    TASSERT(strstr(fixture_fatal_text, "command 999 is not implemented") != NULL);
    ipc_session_release(root);
    empty_everything();
}

static void case_table_limit(void) {
    IpcDomain domain = {0};
    domain.next = DOMAIN_OBJECT_LIMIT;
    IpcSession *object = object_new("fixture object", chat_user_handler, NULL, NULL);
    fixture_catch_fatal = 1;
    if (!setjmp(fixture_fatal_jump)) { (void)domain_add(&domain, object); TASSERT(0); }
    fixture_catch_fatal = 0;
    TASSERT(strstr(fixture_fatal_text, "domain object limit") != NULL);
    ipc_session_release(object);
    empty_everything(); // fatal is process-terminal in production; the local domain has no stored objects
}

static void case_malformed_domain(void) {
    uint32_t handle = ipc_session_new("fixture chat", chat_user_handler, NULL, NULL);
    IpcSession *root = kern_session_acquire(handle);
    uint32_t id = convert_wire(root);
    uint32_t message[64];
    wire_domain(message, 1, id, 0, NULL, 0);
    ((unsigned char *)message)[18] = 0xff; // data_size exceeds the bounded message
    fixture_catch_fatal = 1;
    if (!setjmp(fixture_fatal_jump)) { dispatch(root, message); TASSERT(0); }
    fixture_catch_fatal = 0;
    TASSERT(strstr(fixture_fatal_text, "domain request type/size") != NULL);
    ipc_session_release(root);
    kern_handle_close(handle);
    empty_everything();
}

static uint32_t gsv_bootstrap(IpcSession **result) {
    uint32_t handle = gsv_user_session_new();
    IpcSession *root = kern_session_acquire(handle);
    uint64_t boot[6] = {UINT64_C(0x1256789), UINT64_C(0x8000000000000005), 0, 0, 0, 0};
    uint32_t message[64];
    wire_tipc(message, 4, boot, sizeof boot, 1, UINT32_C(0xffff8001));
    dispatch(root, message);
    tipc_success(message, 0);
    GsvUser *state = root->state;
    TASSERT(state->booted && state->aruid == boot[0] && state->boot_version == boot[1]);
    TASSERT(state->process == UINT32_C(0xffff8001) && state->remote_usage == 0);
    wire_tipc(message, 11, NULL, 0, 1, UINT32_C(0xffff8001));
    dispatch(root, message);
    tipc_success(message, 0);
    TASSERT(state->registered);
    wire_tipc(message, 101, NULL, 0, 0, 0);
    dispatch(root, message);
    tipc_success(message, 0);
    TASSERT(state->context);
    wire_tipc(message, 1501, NULL, 0, 0, 0);
    dispatch(root, message);
    tipc_success(message, 0);
    TASSERT(state->intermodule_created && !state->intermodule_ready);
    wire_tipc(message, 1502, NULL, 0, 0, 0);
    dispatch(root, message);
    tipc_success(message, 0);
    TASSERT(state->intermodule_ready);
    *result = root;
    return handle;
}

static uint32_t gsv_open_chat(IpcSession *root, IpcSession **result) {
    uint32_t message[64];
    wire_tipc(message, 1201, NULL, 0, 0, 0);
    dispatch(root, message);
    tipc_success(message, 0);
    wire_tipc(message, 1202, NULL, 0, 0, 0);
    dispatch(root, message);
    uint32_t handle = tipc_success(message, 1);
    IpcSession *chat = kern_session_acquire(handle);
    TASSERT(chat && !chat->domain && !strcmp(chat->name, "gsv ChatSession"));
    *result = chat;
    return handle;
}

static uint32_t gsv_open_display(IpcSession *root, IpcSession **result) {
    uint32_t message[64];
    wire_tipc(message, 301, NULL, 0, 0, 0);
    dispatch(root, message);
    tipc_success(message, 0);
    uint64_t version = UINT64_C(0x300020000);
    wire_tipc(message, 305, &version, sizeof version, 0, 0);
    dispatch(root, message);
    uint32_t handle = tipc_success(message, 1);
    IpcSession *display = kern_session_acquire(handle);
    TASSERT(display && !display->domain && !strcmp(display->name, "gsv DisplaySession"));
    TASSERT(((GsvDisplay *)display->state)->version == version);
    *result = display;
    return handle;
}

static void case_gsv_lifecycle(void) {
    IpcSession *root, *chat;
    uint32_t handle = gsv_bootstrap(&root);
    uint32_t chat_handle = gsv_open_chat(root, &chat);
    uint32_t message[64], ids[2], events[3];
    for (unsigned i = 0; i < 2; i++) {
        wire_tipc(message, 1, NULL, 0, 0, 0);
        dispatch(chat, message);
        ids[i] = tipc_success(message, 0);
    }
    TASSERT(ids[0] != ids[1] && fixture_event_count == 2 && fixture_writable_count == 2);
    for (unsigned i = 0; i < 3; i++) {
        uint32_t id = ids[i == 2];
        wire_tipc(message, 11, &id, sizeof id, 0, 0);
        dispatch(chat, message);
        events[i] = tipc_success(message, 1);
    }
    TASSERT(events[0] != events[1] && fixture_handles[events[0]].object == fixture_handles[events[1]].object);
    TASSERT(fixture_handles[events[0]].object != fixture_handles[events[2]].object);
    TASSERT(!((FixtureEvent *)fixture_handles[events[0]].object)->signalled);
    ipc_session_release(root);
    kern_handle_close(handle);
    ipc_session_release(chat);
    kern_handle_close(chat_handle);
    TASSERT(ipc_sessions_live() == 0 && fixture_writable_count == 0 && fixture_event_count == 2);
    for (unsigned i = 0; i < 3; i++) kern_handle_close(events[i]);
    empty_everything();
}

static void case_gsv_bad_order(void) {
    uint32_t handle = gsv_user_session_new();
    IpcSession *root = kern_session_acquire(handle);
    uint32_t message[64];
    wire_tipc(message, 101, NULL, 0, 0, 0);
    fixture_catch_fatal = 1;
    if (!setjmp(fixture_fatal_jump)) { dispatch(root, message); TASSERT(0); }
    fixture_catch_fatal = 0;
    TASSERT(strstr(fixture_fatal_text, "command 101 is not implemented"));
    TASSERT(!((GsvUser *)root->state)->context);
    ipc_session_release(root);
    kern_handle_close(handle);
    empty_everything();
}

static void case_gsv_duplicate(void) {
    const uint32_t commands[] = {4, 11, 101, 1501, 1502};
    uint64_t boot[6] = {1, UINT64_C(0x8000000000000005), 0, 0, 0, 0};
    for (unsigned i = 0; i < sizeof commands / sizeof *commands; i++) {
        IpcSession *root;
        uint32_t handle = gsv_bootstrap(&root);
        uint32_t message[64];
        unsigned command = commands[i];
        wire_tipc(message, command, command == 4 ? boot : NULL, command == 4 ? sizeof boot : 0,
                  command == 4 || command == 11, UINT32_C(0xffff8001));
        fixture_catch_fatal = 1;
        if (!setjmp(fixture_fatal_jump)) { dispatch(root, message); TASSERT(0); }
        fixture_catch_fatal = 0;
        TASSERT(strstr(fixture_fatal_text, "is not implemented"));
        // Production fatal terminates the process while its lock is held. The
        // longjmp fixture discards this terminal state; it must never reuse it.
        ipc_session_release(root);
        kern_handle_close(handle);
        empty_everything();
    }
}

static void case_gsv_bad_boot(void) {
    uint32_t handle = gsv_user_session_new();
    IpcSession *root = kern_session_acquire(handle);
    uint32_t message[64];
    uint64_t boot[6] = {1, UINT64_C(0x8000000000000006), 0, 0, 0, 0};
    wire_tipc(message, 4, boot, sizeof boot, 1, UINT32_C(0xffff8001));
    fixture_catch_fatal = 1;
    if (!setjmp(fixture_fatal_jump)) { dispatch(root, message); TASSERT(0); }
    fixture_catch_fatal = 0;
    TASSERT(strstr(fixture_fatal_text, "command 4 is not implemented"));
    TASSERT(!((GsvUser *)root->state)->booted);
    ipc_session_release(root);
    kern_handle_close(handle);
    empty_everything();
    handle = gsv_user_session_new();
    root = kern_session_acquire(handle);
    wire_tipc(message, 4, boot, 8, 1, UINT32_C(0xffff8001));
    fixture_catch_fatal = 1;
    if (!setjmp(fixture_fatal_jump)) { dispatch(root, message); TASSERT(0); }
    fixture_catch_fatal = 0;
    TASSERT(strstr(fixture_fatal_text, "beyond 8 bytes"));
    ipc_session_release(root);
    kern_handle_close(handle);
    empty_everything();
}

static void case_gsv_data_path_rejected(void) {
    IpcSession *root;
    uint32_t handle = gsv_bootstrap(&root);
    IpcRequest request = {.type = 20, .cmd = 999, .na = 1};
    IpcResponse response = {0};
    fixture_catch_fatal = 1;
    if (!setjmp(fixture_fatal_jump)) { user_handler(root, &request, &response); TASSERT(0); }
    fixture_catch_fatal = 0;
    TASSERT(strstr(fixture_fatal_text, "command 999 is not implemented"));
    ipc_session_release(root);
    kern_handle_close(handle);
    empty_everything();
    handle = gsv_bootstrap(&root);
    request.na = 0;
    fixture_catch_fatal = 1;
    if (!setjmp(fixture_fatal_jump)) { user_handler(root, &request, &response); TASSERT(0); }
    fixture_catch_fatal = 0;
    TASSERT(strstr(fixture_fatal_text, "command 999 is not implemented"));
    ipc_session_release(root);
    kern_handle_close(handle);
    empty_everything();
}

static void case_gsv_bad_subscription(void) {
    IpcSession *root, *chat;
    uint32_t handle = gsv_bootstrap(&root);
    uint32_t chat_handle = gsv_open_chat(root, &chat);
    uint32_t message[64], nonexistent = 7;
    wire_tipc(message, 11, &nonexistent, sizeof nonexistent, 0, 0);
    fixture_catch_fatal = 1;
    if (!setjmp(fixture_fatal_jump)) { dispatch(chat, message); TASSERT(0); }
    fixture_catch_fatal = 0;
    TASSERT(strstr(fixture_fatal_text, "invalid subscription 7"));
    TASSERT(fixture_event_count == 0);
    ipc_session_release(root);
    kern_handle_close(handle);
    ipc_session_release(chat);
    kern_handle_close(chat_handle);
    empty_everything();
}

static void case_gsv_display_endpoints(void) {
    IpcSession *root, *display;
    uint32_t handle = gsv_bootstrap(&root);
    uint32_t display_handle = gsv_open_display(root, &display);
    GsvDisplay *state = display->state;
    uint32_t message[64];
    uint64_t display_data[2] = {UINT64_C(0x1122334455667788), 0};
    wire_tipc(message, 101, display_data, 12, 0, 0);
    dispatch(display, message);
    tipc_success(message, 0);
    TASSERT(state->display_open[0] && state->display_names[0] == display_data[0]);
    struct { uint64_t tag; uint32_t a, b; } endpoints = {UINT64_C(0xaabbccddeeff0011), 2, 3};
    wire_tipc(message, 201, &endpoints, sizeof endpoints, 0, 0);
    dispatch(display, message);
    tipc_success(message, 0);
    TASSERT(state->layers[2].open && state->layers[3].open);
    TASSERT(state->layers[2].client_tag == endpoints.tag && state->layers[3].client_tag == endpoints.tag);
    TASSERT(state->layers[2].peer == 3 && state->layers[3].peer == 2);
    TASSERT(state->layers[2].role == 0 && state->layers[3].role == 1);
    uint32_t notification = 2, slot = 0, events[3];
    for (unsigned i = 0; i < 2; i++) {
        wire_tipc(message, 11, &notification, sizeof notification, 0, 0);
        dispatch(display, message);
        events[i] = tipc_success(message, 1);
    }
    wire_tipc(message, 31, &slot, sizeof slot, 0, 0);
    dispatch(display, message);
    events[2] = tipc_success(message, 1);
    TASSERT(fixture_handles[events[0]].object == fixture_handles[events[1]].object);
    TASSERT(fixture_handles[events[0]].object != fixture_handles[events[2]].object);
    TASSERT(fixture_writable_count == 2 && fixture_event_count == 2);
    ipc_session_release(root);
    kern_handle_close(handle);
    ipc_session_release(display);
    kern_handle_close(display_handle);
    TASSERT(fixture_writable_count == 0 && fixture_event_count == 2);
    for (unsigned i = 0; i < 3; i++) kern_handle_close(events[i]);
    empty_everything();
}

static void case_gsv_display_bad_endpoints(void) {
    for (unsigned kind = 0; kind < 3; kind++) {
        IpcSession *root, *display;
        uint32_t handle = gsv_bootstrap(&root);
        uint32_t display_handle = gsv_open_display(root, &display);
        uint32_t message[64];
        struct { uint64_t tag; uint32_t a, b; } endpoints = {UINT64_C(0xfedcba9876543210), 2, 3};
        if (kind == 2) {
            wire_tipc(message, 201, &endpoints, sizeof endpoints, 0, 0);
            dispatch(display, message);
            tipc_success(message, 0);
            endpoints.a = 3;
            endpoints.b = 4;
        } else if (kind == 1) endpoints.a = 1024;
        else endpoints.b = endpoints.a;
        wire_tipc(message, 201, &endpoints, sizeof endpoints, 0, 0);
        fixture_catch_fatal = 1;
        if (!setjmp(fixture_fatal_jump)) { dispatch(display, message); TASSERT(0); }
        fixture_catch_fatal = 0;
        TASSERT(strstr(fixture_fatal_text, "command 201 is not implemented"));
        TASSERT(!((GsvDisplay *)display->state)->layers[4].open);
        // This session represents a terminated process after the fatal; discard it.
        ipc_session_release(root);
        kern_handle_close(handle);
        ipc_session_release(display);
        kern_handle_close(display_handle);
        empty_everything();
    }
}

static void case_gsv_local_frame_policy(void) {
    TASSERT(_putenv_s("HWAOI_GSV_LOCAL_FRAME_POLICY", "1") == 0);
    IpcSession *root, *other;
    uint32_t handle = gsv_bootstrap(&root), second = gsv_bootstrap(&other);
    GsvUser *state = root->state, *other_state = other->state;
    TASSERT(!state->graphics_frame_policy_set && !other_state->graphics_frame_policy_set);
    unsigned logs_before=gsv_policy_logs, tipc_before=gsv_tipc_logs;
    const uint32_t modes[] = {0, 0x11e, 0x13c, 0x178, 0x13c, 0x13c, 0};
    for (unsigned i = 0; i < sizeof(modes) / sizeof(modes[0]); i++) {
        uint32_t message[64];
        wire_tipc(message, 1111, &modes[i], 4, 0, 0);
        dispatch(root, message);
        tipc_success(message, 0);
        TASSERT(state->graphics_frame_policy_set && state->graphics_frame_policy == modes[i]);
        TASSERT(!other_state->graphics_frame_policy_set); // session-local, not a global pacer setting
    }
    TASSERT(gsv_policy_logs-logs_before==6 && gsv_tipc_logs==tipc_before);
    // Explicit IPC tracing retains every request, including repeated tokens.
    rt_trace|=RT_TRACE_IPC;
    for(unsigned i=0;i<2;i++){
        uint32_t message[64],mode=0x13c;wire_tipc(message,1111,&mode,4,0,0);
        dispatch(root,message);tipc_success(message,0);
    }
    rt_trace&=~RT_TRACE_IPC;
    TASSERT(gsv_policy_logs-logs_before==8 && gsv_tipc_logs-tipc_before==2);
    ipc_session_release(other); kern_handle_close(second);
    ipc_session_release(root); kern_handle_close(handle);
    TASSERT(_putenv_s("HWAOI_GSV_LOCAL_FRAME_POLICY", "") == 0);
    empty_everything();
}

static void case_gsv_frame_policy_default(void) {
    const char *settings[] = {"", "0", "true", "01"};
    for (unsigned i = 0; i < sizeof(settings) / sizeof(settings[0]); i++) {
        TASSERT(_putenv_s("HWAOI_GSV_LOCAL_FRAME_POLICY", settings[i]) == 0);
        IpcSession *root;
        uint32_t handle = gsv_bootstrap(&root), mode = 0x13c, message[64];
        wire_tipc(message, 1111, &mode, 4, 0, 0);
        fixture_catch_fatal = 1;
        if (!setjmp(fixture_fatal_jump)) { dispatch(root, message); TASSERT(0); }
        fixture_catch_fatal = 0;
        TASSERT(strstr(fixture_fatal_text, "command 1111 is not implemented"));
        TASSERT(!((GsvUser *)root->state)->graphics_frame_policy_set);
        ipc_session_release(root); kern_handle_close(handle); empty_everything();
    }
    TASSERT(_putenv_s("HWAOI_GSV_LOCAL_FRAME_POLICY", "") == 0);
}

static void case_gsv_frame_policy_bad(void) {
    TASSERT(_putenv_s("HWAOI_GSV_LOCAL_FRAME_POLICY", "1") == 0);
    for (unsigned kind = 0; kind < 7; kind++) {
        IpcSession *root;
        uint32_t handle;
        if (kind == 2) { handle = gsv_user_session_new(); root = kern_session_acquire(handle); }
        else handle = gsv_bootstrap(&root);
        uint32_t data[2] = {kind == 0 ? 1u : kind == 1 ? 0x11fu : 0x13cu, 0};
        IpcRequest request = {.type=1127, .cmd=1111, .data=(const uint8_t *)data,
                              .data_size=kind == 3 ? 0 : kind == 4 ? 8 : 4,
                              .ncopy=kind == 5, .has_pid=kind == 6};
        IpcResponse response = {0};
        fixture_catch_fatal = 1;
        if (!setjmp(fixture_fatal_jump)) { root->handler(root, &request, &response); TASSERT(0); }
        fixture_catch_fatal = 0;
        TASSERT(!((GsvUser *)root->state)->graphics_frame_policy_set);
        TASSERT(!response.data_size && !response.ncopy && !response.nmove);
        ipc_session_release(root); kern_handle_close(handle); empty_everything();
    }
    TASSERT(_putenv_s("HWAOI_GSV_LOCAL_FRAME_POLICY", "") == 0);
}

int main(int argc, char **argv) {
    TASSERT(argc == 2);
    if (!strcmp(argv[1], "lifecycle")) case_lifecycle();
    else if (!strcmp(argv[1], "copy-root")) case_copy_root();
    else if (!strcmp(argv[1], "native-move")) case_native_move();
    else if (!strcmp(argv[1], "copy-convert")) case_copy_conversion_isolation();
    else if (!strcmp(argv[1], "clone-convert")) case_plain_clone_conversion_isolation();
    else if (!strcmp(argv[1], "output-offset")) case_output_object_offset();
    else if (!strcmp(argv[1], "clone-ex")) case_clone_ex();
    else if (!strcmp(argv[1], "invalid-id")) case_invalid_id();
    else if (!strcmp(argv[1], "unknown-chat")) case_unknown_chat();
    else if (!strcmp(argv[1], "table-limit")) case_table_limit();
    else if (!strcmp(argv[1], "malformed-domain")) case_malformed_domain();
    else if (!strcmp(argv[1], "gsv-lifecycle")) case_gsv_lifecycle();
    else if (!strcmp(argv[1], "gsv-order")) case_gsv_bad_order();
    else if (!strcmp(argv[1], "gsv-duplicate")) case_gsv_duplicate();
    else if (!strcmp(argv[1], "gsv-boot")) case_gsv_bad_boot();
    else if (!strcmp(argv[1], "gsv-data")) case_gsv_data_path_rejected();
    else if (!strcmp(argv[1], "gsv-subscription")) case_gsv_bad_subscription();
    else if (!strcmp(argv[1], "gsv-display")) case_gsv_display_endpoints();
    else if (!strcmp(argv[1], "gsv-display-bad")) case_gsv_display_bad_endpoints();
    else if (!strcmp(argv[1], "gsv-frame-local")) case_gsv_local_frame_policy();
    else if (!strcmp(argv[1], "gsv-frame-default")) case_gsv_frame_policy_default();
    else if (!strcmp(argv[1], "gsv-frame-bad")) case_gsv_frame_policy_bad();
    else TASSERT(0);
    puts("synthetic IPC/domain check passed");
    return 0;
}
