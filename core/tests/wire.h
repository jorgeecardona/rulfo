/* Two peers joined by byte queues in memory. */
#ifndef RULFO_TEST_WIRE_H
#define RULFO_TEST_WIRE_H

#include "check.h"
#include "rulfo.h"

#include <string.h>

#define QUEUE 8192

/* A peer, the memory it lives in, and the bytes on their way to it. */
typedef struct {
    rf_peer *peer;
    void *memory;
    uint8_t in[QUEUE];
    size_t len;
} end;

static inline void open_end(end *e, const rf_config *config) {
    size_t size = rf_peer_size(config);
    CHECK(size > 0);
    e->memory = malloc(size);
    CHECK(e->memory != NULL);
    CHECK(rf_peer_init(e->memory, size, config, &e->peer) == RF_OK);
    e->len = 0;
}

static inline void close_end(end *e) { free(e->memory); }

static inline void put(end *to, const uint8_t *bytes, size_t len) {
    CHECK(len <= QUEUE - to->len);
    if (len > 0) {
        memcpy(to->in + to->len, bytes, len);
        to->len += len;
    }
}

/* Moves what the core of `from` wants sent into the queue of `to`. */
static inline void flush(end *from, end *to) {
    uint8_t out[256];
    size_t n;
    while ((n = rf_output(from->peer, out, sizeof out)) > 0) {
        put(to, out, n);
    }
}

/* Feeds `e` until it gives an event or needs more bytes. The event's key and body point into
 * `scratch`, which must hold QUEUE bytes. */
static inline rf_status step(end *e, uint64_t now, uint8_t *scratch, rf_event *event) {
    for (;;) {
        memcpy(scratch, e->in, e->len);
        size_t used = 0;
        rf_status status = rf_input(e->peer, scratch, e->len, now, &used, event);
        CHECK(used <= e->len);
        memmove(e->in, e->in + used, e->len - used);
        e->len -= used;
        if (status != RF_OK || event->kind != RF_EV_NONE) {
            return status;
        }
    }
}

/* Connects both ends at `now` and runs the handshake. */
static inline void join(end *a, end *b, uint64_t now_a, uint64_t now_b) {
    uint8_t scratch[QUEUE];
    rf_event event;
    a->len = 0;
    b->len = 0;
    rf_connected(a->peer, now_a);
    rf_connected(b->peer, now_b);
    flush(a, b);
    flush(b, a);
    CHECK(step(a, now_a, scratch, &event) == RF_NEED_MORE);
    CHECK(step(b, now_b, scratch, &event) == RF_NEED_MORE);
    flush(a, b);
    flush(b, a);
    CHECK(step(a, now_a, scratch, &event) == RF_OK && event.kind == RF_EV_READY);
    CHECK(step(b, now_b, scratch, &event) == RF_OK && event.kind == RF_EV_READY);
}

#endif
