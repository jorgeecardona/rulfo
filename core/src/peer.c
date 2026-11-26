#include "rulfo.h"

#include <stdalign.h>
#include <stdbool.h>
#include <string.h>

#define PREFACE_AND_CLOCK (RF_PREFACE_SIZE + RF_HEADER_SIZE)

typedef enum {
    LINK_DOWN,    /* no connection, or one that a protocol error ended */
    LINK_PREFACE, /* waiting for the other preface */
    LINK_CLOCK,   /* waiting for the other CLOCK */
    LINK_READY
} link_state;

struct rf_peer {
    rf_config config;

    /* The current connection. Everything here is reset by rf_connected. */
    link_state link;
    uint64_t connected_at;
    uint16_t capabilities; /* of the other side */
    uint64_t their_clock;  /* the argument of their CLOCK */
    uint64_t read_at;      /* our clock when we read it */
    size_t skip;           /* bytes of a dropped frame still to come */

    uint8_t *out; /* config.max_out bytes */
    size_t out_len;
};

static uint64_t add(uint64_t a, uint64_t b) { return a > UINT64_MAX - b ? UINT64_MAX : a + b; }

static uint64_t since(uint64_t now, uint64_t then) { return now > then ? now - then : 0; }

void rf_config_default(rf_config *config) {
    *config = (rf_config){.max_length = RF_DEFAULT_MAX_LENGTH,
                          .max_out = 1024,
                          .handshake_ms = 5000,
                          .keep_ms = 60000};
}

size_t rf_peer_size(const rf_config *config) {
    /* A keep time below the handshake limit lets a request run twice (docs/operations.md). */
    if (config->max_out < PREFACE_AND_CLOCK || config->handshake_ms == 0 ||
        config->keep_ms < config->handshake_ms) {
        return 0;
    }
#if SIZE_MAX <= UINT32_MAX
    if (config->max_out > SIZE_MAX - sizeof(struct rf_peer)) {
        return 0;
    }
#endif
    return sizeof(struct rf_peer) + config->max_out;
}

rf_status rf_peer_init(void *memory, size_t len, const rf_config *config, rf_peer **peer) {
    size_t size = rf_peer_size(config);
    if (size == 0) {
        return RF_ERR_CONFIG;
    }
    if (memory == NULL || (uintptr_t)memory % alignof(max_align_t) != 0 || len < size) {
        return RF_ERR_MEMORY;
    }
    rf_peer *made = memory;
    *made = (rf_peer){.config = *config, .link = LINK_DOWN};
    made->out = (uint8_t *)memory + sizeof *made;
    *peer = made;
    return RF_OK;
}

static void reset_link(rf_peer *peer, link_state link) {
    peer->link = link;
    peer->connected_at = 0;
    peer->capabilities = 0;
    peer->their_clock = 0;
    peer->read_at = 0;
    peer->skip = 0;
    peer->out_len = 0;
}

void rf_connected(rf_peer *peer, uint64_t now) {
    reset_link(peer, LINK_PREFACE);
    peer->connected_at = now;
    /* This core always drops repeated ids. */
    rf_preface_encode(RF_CAP_DROPS_REPEATS, peer->out);
    peer->out_len = RF_PREFACE_SIZE;
}

void rf_disconnected(rf_peer *peer) { reset_link(peer, LINK_DOWN); }

/* Queues a frame without key or body. False when there is no room for it. */
static bool queue(rf_peer *peer, const rf_header *header) {
    if (peer->config.max_out - peer->out_len < RF_HEADER_SIZE) {
        return false;
    }
    if (rf_header_encode(header, peer->config.max_length, peer->out + peer->out_len) != RF_OK) {
        return false;
    }
    peer->out_len += RF_HEADER_SIZE;
    return true;
}

size_t rf_output(rf_peer *peer, uint8_t *out, size_t cap) {
    size_t n = peer->out_len < cap ? peer->out_len : cap;
    if (n > 0) {
        memcpy(out, peer->out, n);
        memmove(peer->out, peer->out + n, peer->out_len - n);
        peer->out_len -= n;
    }
    return n;
}

static rf_status close_link(rf_peer *peer, rf_status status, rf_event *event) {
    reset_link(peer, LINK_DOWN);
    event->kind = RF_EV_CLOSE;
    event->status = status;
    return status;
}

static bool handshake_is_late(const rf_peer *peer, uint64_t now) {
    return (peer->link == LINK_PREFACE || peer->link == LINK_CLOCK) &&
           since(now, peer->connected_at) > peer->config.handshake_ms;
}

static rf_status read_preface(rf_peer *peer, const uint8_t *in, size_t len, uint64_t now,
                              size_t *used, rf_event *event) {
    uint16_t capabilities = 0;
    rf_status status = rf_preface_decode(in, len, &capabilities);
    if (status == RF_NEED_MORE) {
        return status;
    }
    if (status != RF_OK) {
        return close_link(peer, status, event);
    }
    /* Our CLOCK is sent after their preface was read: their wait for it bounds its delay. */
    rf_header clock = {.kind = RF_CLOCK, .argument = now};
    if (!queue(peer, &clock)) {
        return RF_WOULD_BLOCK;
    }
    peer->capabilities = capabilities;
    peer->link = LINK_CLOCK;
    *used = RF_PREFACE_SIZE;
    return RF_OK;
}

rf_status rf_input(rf_peer *peer, const uint8_t *in, size_t len, uint64_t now, size_t *used,
                   rf_event *event) {
    *used = 0;
    *event = (rf_event){.kind = RF_EV_NONE};
    if (peer->link == LINK_DOWN) {
        return RF_ERR_STATE;
    }
    if (handshake_is_late(peer, now)) {
        return close_link(peer, RF_ERR_HANDSHAKE, event);
    }
    if (len == 0) {
        return RF_NEED_MORE;
    }
    if (peer->skip > 0) {
        *used = len < peer->skip ? len : peer->skip;
        peer->skip -= *used;
        return RF_OK;
    }
    if (peer->link == LINK_PREFACE) {
        return read_preface(peer, in, len, now, used, event);
    }

    rf_header header;
    rf_status status = rf_header_decode(in, len, peer->config.max_length, &header);
    if (status == RF_NEED_MORE) {
        return status;
    }
    if (status != RF_OK) {
        return close_link(peer, status, event);
    }
    if ((peer->link == LINK_CLOCK) != (header.kind == RF_CLOCK)) {
        return close_link(peer, RF_ERR_HANDSHAKE, event);
    }
    if (header.kind == RF_CLOCK) {
        peer->their_clock = header.argument;
        peer->read_at = now;
        peer->link = LINK_READY;
        *used = RF_HEADER_SIZE;
        event->kind = RF_EV_READY;
        return RF_OK;
    }
    /* No operation reads this frame yet: drop it. */
    *used = RF_HEADER_SIZE;
    peer->skip = header.key_len + header.body_len;
    return RF_OK;
}

void rf_poll(rf_peer *peer, uint64_t now, rf_event *event) {
    *event = (rf_event){.kind = RF_EV_NONE};
    if (handshake_is_late(peer, now)) {
        (void)close_link(peer, RF_ERR_HANDSHAKE, event);
    }
}

uint64_t rf_next_time(const rf_peer *peer) {
    if (peer->link == LINK_PREFACE || peer->link == LINK_CLOCK) {
        return add(peer->connected_at, add(peer->config.handshake_ms, 1));
    }
    return UINT64_MAX;
}
