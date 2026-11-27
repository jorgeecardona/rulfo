/* libFuzzer target for rf_peer: the input is a list of calls and received frames. The target
 * keeps its own list of open requests and remembered ids and aborts when the core breaks a rule
 * of docs/operations.md. */
#include "rulfo.h"

#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#define REQUIRE(condition) \
    do {                   \
        if (!(condition)) { \
            abort();       \
        }                  \
    } while (0)

#define IDS 4
#define BUFFER 512

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

typedef struct {
    const uint8_t *data;
    size_t len;
} source;

static uint8_t take(source *s) {
    if (s->len == 0) {
        return 0;
    }
    s->len--;
    return *s->data++;
}

typedef struct {
    rf_peer *peer;
    uint64_t now;
    uint8_t in[BUFFER];
    size_t in_len;
    bool open[IDS];       /* requests we sent that have no outcome */
    uint64_t replies[IDS];
    uint64_t deadline[IDS];
    bool known[IDS];      /* ids the core remembers as replier */
    bool marked[IDS];     /* the copy that was taken was marked as sent again */
    uint64_t brought[IDS]; /* the connection that brought it */
    uint64_t connection;
    bool greeted;
    uint8_t sent[256]; /* bytes taken from rf_output and not checked yet */
    size_t sent_len;
    bool preface_sent;
    size_t unmarked[IDS]; /* unmarked requests received since the connect, less busy ends */
    bool clean; /* only a greeting and then whole well-formed frames since the connect */
} world;

static rf_id id_of(uint8_t n) {
    rf_id id = {{0}};
    id.bytes[0] = n;
    return id;
}

static void check_event(world *w, const rf_event *e) {
    uint8_t n = e->id.bytes[0];
    switch (e->kind) {
    case RF_EV_REPLY:
        REQUIRE(n < IDS && w->open[n] && e->number == w->replies[n]);
        REQUIRE(w->now < w->deadline[n]); /* nothing arrives after the deadline */
        /* The frames that frame_from makes say their number in the first body byte. */
        REQUIRE(!w->clean || e->body.len == 0 || e->body.ptr[0] == (uint8_t)e->number);
        w->replies[n]++;
        break;
    case RF_EV_OUTCOME:
        REQUIRE(n < IDS && w->open[n] && e->number == w->replies[n]);
        w->open[n] = false;
        break;
    case RF_EV_RESEND:
        REQUIRE(n < IDS && w->open[n]);
        break;
    case RF_EV_REQUEST:
        /* A remembered id never runs again. Ids outside our range are not tracked. */
        if (n < IDS) {
            REQUIRE(!w->known[n]);
            w->known[n] = true;
            w->marked[n] = (e->flags & RF_REQUEST_REPEAT) != 0;
            w->brought[n] = w->connection;
        }
        REQUIRE(e->deadline > w->now);
        break;
    case RF_EV_REPEAT:
        REQUIRE(n >= IDS || w->known[n]);
        break;
    case RF_EV_FORGOT:
        if (n < IDS) {
            REQUIRE(w->known[n]);
            w->known[n] = false;
        }
        break;
    default:
        break;
    }
}

static void read_input(world *w) {
    for (;;) {
        /* A copy of exactly the waiting bytes, so the sanitizer sees a read past them. */
        uint8_t *in = malloc(w->in_len == 0 ? 1 : w->in_len);
        REQUIRE(in != NULL);
        memcpy(in, w->in, w->in_len);
        size_t used = 0;
        rf_event event;
        rf_status status = rf_input(w->peer, in, w->in_len, w->now, &used, &event);
        REQUIRE(used <= w->in_len);
        REQUIRE(status == RF_OK || used == 0);
        if (event.kind == RF_EV_REQUEST) {
            REQUIRE(event.key.ptr >= in && event.body.ptr + event.body.len <= in + used);
        }
        if (event.kind == RF_EV_REPLY) {
            REQUIRE(event.body.ptr >= in && event.body.ptr + event.body.len <= in + used);
        }
        check_event(w, &event);
        free(in);
        memmove(w->in, w->in + used, w->in_len - used);
        w->in_len -= used;
        if (status != RF_OK) {
            if (status != RF_NEED_MORE && status != RF_WOULD_BLOCK) {
                /* A well-formed stream is never a protocol error, apart from a late CLOCK. */
                REQUIRE(!w->clean || status == RF_ERR_STATE || status == RF_ERR_HANDSHAKE);
                w->in_len = 0;
            }
            return;
        }
    }
}

/* Adds all of `bytes`, or nothing. */
static bool put(world *w, const uint8_t *bytes, size_t len) {
    if (len > BUFFER - w->in_len) {
        return false;
    }
    if (len > 0) {
        memcpy(w->in + w->in_len, bytes, len);
        w->in_len += len;
    }
    return true;
}

/* Checks what the core wants sent: after the preface, only the CLOCK and busy ends. A busy end
 * answers an unmarked request (rule 11). */
static void check_output(world *w, size_t cap) {
    size_t room = sizeof w->sent - w->sent_len;
    w->sent_len += rf_output(w->peer, w->sent + w->sent_len, cap < room ? cap : room);
    for (;;) {
        size_t size = w->preface_sent ? RF_HEADER_SIZE : RF_PREFACE_SIZE;
        if (w->sent_len < size) {
            return;
        }
        if (!w->preface_sent) {
            uint16_t capabilities = 0;
            REQUIRE(rf_preface_decode(w->sent, size, &capabilities) == RF_OK);
            w->preface_sent = true;
        } else {
            rf_header header;
            REQUIRE(rf_header_decode(w->sent, size, 0, &header) == RF_OK);
            if (header.kind != RF_CLOCK) {
                REQUIRE(header.kind == RF_END && header.flags == RF_END_BUSY);
                uint8_t n = header.id.bytes[0];
                if (w->clean && n < IDS) {
                    REQUIRE(w->unmarked[n] > 0);
                    w->unmarked[n]--;
                }
            }
        }
        memmove(w->sent, w->sent + size, w->sent_len - size);
        w->sent_len -= size;
    }
}

static void frame_from(world *w, source *s) {
    uint8_t body[16] = {0};
    rf_frame frame = {0};
    frame.kind = (uint8_t)(1 + take(s) % 4);
    frame.flags = (uint8_t)(take(s) % 4);
    frame.id = id_of((uint8_t)(take(s) % IDS));
    frame.argument = take(s) % 2 ? take(s) % 4 : w->now + take(s);
    if (frame.kind == RF_REQUEST) {
        frame.flags &= RF_REQUEST_REPEAT;
        frame.key = (rf_bytes){.ptr = (const uint8_t *)"k", .len = 1};
    } else if (frame.kind == RF_REPLY) {
        frame.flags &= RF_REPLY_ERROR;
    }
    if (frame.kind == RF_REQUEST || frame.kind == RF_REPLY) {
        body[0] = (uint8_t)frame.argument;
        frame.body = (rf_bytes){.ptr = body, .len = take(s) % sizeof body};
    } else if (frame.flags != 0) {
        frame.argument = 0;
    }
    if (frame.kind == RF_CLOCK) {
        frame.flags = 0;
        frame.id = id_of(0);
    }
    uint8_t out[128];
    size_t written = 0;
    if (rf_frame_encode(&frame, 64, out, sizeof out, &written) != RF_OK) {
        return;
    }
    if (!w->greeted || frame.kind == RF_CLOCK) {
        w->clean = false;
    }
    /* In two pieces: the first can end inside the header, the key or the body. */
    size_t first = take(s) % (written + 1);
    if (first > BUFFER - w->in_len || written > BUFFER - w->in_len) {
        return;
    }
    if (frame.kind == RF_REQUEST && frame.flags == 0) {
        w->unmarked[frame.id.bytes[0]]++;
    }
    REQUIRE(put(w, out, first));
    read_input(w);
    REQUIRE(put(w, out + first, written - first) || !w->clean);
}

static void connect(world *w) {
    w->connection++;
    w->in_len = 0;
    w->greeted = false;
    w->clean = true;
    w->sent_len = 0;
    w->preface_sent = false;
    memset(w->unmarked, 0, sizeof w->unmarked);
}

static void call(world *w, source *s) {
    uint8_t header[RF_HEADER_SIZE];
    rf_event event;
    uint8_t op = (uint8_t)(take(s) % 15);
    uint8_t n = (uint8_t)(take(s) % IDS);
    switch (op) {
    case 0:
        rf_connected(w->peer, w->now);
        connect(w);
        break;
    case 1:
        rf_disconnected(w->peer);
        connect(w);
        break;
    case 2: { /* the greeting of the other side */
        uint8_t bytes[RF_PREFACE_SIZE + RF_HEADER_SIZE];
        rf_preface_encode((uint16_t)(take(s) % 2), bytes);
        rf_header clock = {.kind = RF_CLOCK, .argument = w->now};
        REQUIRE(rf_header_encode(&clock, 0, bytes + RF_PREFACE_SIZE) == RF_OK);
        if (w->greeted || w->in_len > 0) {
            w->clean = false;
        }
        w->greeted = put(w, bytes, sizeof bytes) || w->greeted;
        read_input(w);
        break;
    }
    case 3:
        frame_from(w, s);
        read_input(w);
        break;
    case 4: { /* raw bytes */
        uint8_t length = (uint8_t)(take(s) % 48);
        for (uint8_t i = 0; i < length; i++) {
            uint8_t byte = take(s);
            w->clean = false;
            (void)put(w, &byte, 1);
        }
        read_input(w);
        break;
    }
    case 5:
        check_output(w, take(s) % 64);
        read_input(w);
        break;
    case 6:
        w->now += take(s) % 2 ? take(s) : (uint64_t)take(s) * 1000;
        break;
    case 7:
        do {
            rf_poll(w->peer, w->now, &event);
            check_event(w, &event);
            if (event.kind == RF_EV_CLOSE) {
                w->in_len = 0;
            }
        } while (event.kind != RF_EV_NONE);
        break;
    case 8: {
        rf_bytes key = {.ptr = (const uint8_t *)"k", .len = 1};
        uint64_t deadline = w->now + take(s) % 4;
        int repeat = take(s) % 2;
        if (rf_request(w->peer, id_of(n), key, take(s) % 80, deadline, repeat, w->now, header) ==
            RF_OK) {
            REQUIRE(deadline > w->now);
            if (!w->open[n]) {
                w->open[n] = true;
                w->replies[n] = 0;
                w->deadline[n] = deadline;
            }
        }
        break;
    }
    case 9: {
        rf_status status = rf_reply(w->peer, id_of(n), take(s) % 80, take(s) % 3, header);
        REQUIRE(w->known[n] || status == RF_ERR_UNKNOWN_ID);
        break;
    }
    case 10: {
        rf_status status = rf_end(w->peer, id_of(n), header);
        REQUIRE(w->known[n] || status == RF_ERR_UNKNOWN_ID);
        break;
    }
    case 11: {
        rf_status status = rf_refuse(w->peer, id_of(n), (uint8_t)(take(s) % 4), header);
        if (status == RF_OK || status == RF_DROPPED) {
            REQUIRE(w->known[n]);
            w->known[n] = false;
        }
        /* A refusal is sent only for an unmarked copy, on the connection that brought it. */
        if (status == RF_OK) {
            REQUIRE(!w->marked[n] && w->brought[n] == w->connection);
        }
        break;
    }
    case 12:
        (void)rf_reply_again(w->peer, id_of(n), take(s) % 4, take(s) % 80, take(s) % 3, header);
        break;
    case 13: {
        rf_status status = rf_abandon(w->peer, id_of(n));
        REQUIRE((status == RF_OK) == w->open[n]);
        w->open[n] = false;
        break;
    }
    default:
        (void)rf_end_again(w->peer, id_of(n), header);
        (void)rf_next_time(w->peer);
        break;
    }
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    rf_config config;
    rf_config_default(&config);
    /* Small tables and buffers, so the full cases are reached often. */
    config.max_length = 64;
    config.max_out = 40;
    config.max_requests = 2;
    config.max_ids = 2;
    config.max_deadline_ms = 200;
    config.handshake_ms = 100;
    config.keep_ms = 100;
    size_t memory_size = rf_peer_size(&config);
    void *memory = malloc(memory_size);
    REQUIRE(memory != NULL);
    world w = {.now = 1000};
    REQUIRE(rf_peer_init(memory, memory_size, &config, &w.peer) == RF_OK);
    source s = {.data = data, .len = size};
    while (s.len > 0) {
        call(&w, &s);
    }
    /* At the end of time every open request has had its one outcome. */
    w.now = UINT64_MAX;
    rf_event event;
    do {
        rf_poll(w.peer, w.now, &event);
        check_event(&w, &event);
    } while (event.kind != RF_EV_NONE);
    for (size_t n = 0; n < IDS; n++) {
        REQUIRE(!w.open[n]);
    }
    free(memory);
    return 0;
}
