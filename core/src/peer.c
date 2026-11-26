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

/* A request this side sent that has no outcome yet. */
typedef struct {
    rf_id id;
    uint64_t deadline;
    uint64_t replies;  /* received so far, and the number of the next one */
    uint64_t sent_on;  /* the connection that carried the last copy */
    uint64_t asked_on; /* the connection that RF_EV_RESEND was given for */
    bool used;
    bool marked; /* the last copy was marked as sent again */
} request;

/* A request this side took as the replier and still remembers. */
typedef struct {
    rf_id id;
    uint64_t deadline;   /* of the copy that was taken, on our clock */
    uint64_t replies;    /* sent so far, and the number of the next one */
    uint64_t brought_on; /* the connection that brought that copy */
    uint64_t repeated_on; /* the connection that RF_EV_REPEAT was given for */
    bool used;
    bool ended;
    bool marked; /* that copy was marked as sent again */
} taken;

struct rf_peer {
    rf_config config;
    request *requests; /* config.max_requests slots */
    taken *ids;        /* config.max_ids slots */
    uint64_t connection; /* counts calls of rf_connected, the first is 1 */

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
                          .keep_ms = 60000,
                          .max_requests = 64,
                          .max_ids = 4096,
                          .max_deadline_ms = 3600000};
}

size_t rf_peer_size(const rf_config *config) {
    /* A keep time below the handshake limit lets a request run twice (docs/operations.md). */
    if (config->max_out < PREFACE_AND_CLOCK || config->handshake_ms == 0 ||
        config->keep_ms < config->handshake_ms) {
        return 0;
    }
    uint64_t size = sizeof(struct rf_peer);
    size += (uint64_t)config->max_requests * sizeof(request);
    size += (uint64_t)config->max_ids * sizeof(taken);
    size += config->max_out;
#if SIZE_MAX <= UINT32_MAX
    if (size > SIZE_MAX) {
        return 0;
    }
#endif
    return (size_t)size;
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
    /* The arrays follow the struct, the widest alignment first. */
    made->requests = (request *)(void *)(made + 1);
    for (uint32_t i = 0; i < config->max_requests; i++) {
        made->requests[i] = (request){.used = false};
    }
    made->ids = (taken *)(void *)(made->requests + config->max_requests);
    for (uint32_t i = 0; i < config->max_ids; i++) {
        made->ids[i] = (taken){.used = false};
    }
    made->out = (uint8_t *)(made->ids + config->max_ids);
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
    peer->connection++;
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

static request *find_request(rf_peer *peer, const rf_id *id) {
    for (uint32_t i = 0; i < peer->config.max_requests; i++) {
        request *r = &peer->requests[i];
        if (r->used && memcmp(r->id.bytes, id->bytes, sizeof id->bytes) == 0) {
            return r;
        }
    }
    return NULL;
}

static request *free_request(rf_peer *peer) {
    for (uint32_t i = 0; i < peer->config.max_requests; i++) {
        if (!peer->requests[i].used) {
            return &peer->requests[i];
        }
    }
    return NULL;
}

static bool drops_repeats(const rf_peer *peer) {
    return (peer->capabilities & RF_CAP_DROPS_REPEATS) != 0;
}

rf_status rf_request(rf_peer *peer, rf_id id, rf_bytes key, size_t body_len, uint64_t deadline,
                     int repeat, uint64_t now, uint8_t header[RF_HEADER_SIZE]) {
    if (peer->link != LINK_READY) {
        return RF_ERR_NOT_READY;
    }
    if (peer->out_len > 0) {
        return RF_WOULD_BLOCK;
    }
    if (now >= deadline) {
        return RF_ERR_EXPIRED;
    }
    request *open = find_request(peer, &id);
    if (open != NULL &&
        (open->sent_on == peer->connection || open->deadline != deadline || repeat == 0)) {
        return RF_ERR_STATE;
    }
    if (repeat != 0 && !drops_repeats(peer)) {
        return RF_ERR_STATE;
    }
    request *slot = open != NULL ? open : free_request(peer);
    if (slot == NULL) {
        return RF_ERR_FULL;
    }
    if (rf_key_check(key) != RF_OK) {
        return RF_ERR_KEY;
    }
    rf_header frame = {.kind = RF_REQUEST,
                       .flags = repeat != 0 ? RF_REQUEST_REPEAT : 0,
                       .id = id,
                       .argument = deadline,
                       .key_len = key.len,
                       .body_len = body_len};
    rf_status status = rf_header_encode(&frame, peer->config.max_length, header);
    if (status != RF_OK) {
        return status;
    }
    if (open == NULL) {
        *slot = (request){.id = id, .deadline = deadline, .used = true};
    }
    slot->sent_on = peer->connection;
    slot->marked = repeat != 0;
    return RF_OK;
}

/* Reports the outcome and frees the slot. */
static void finish(request *r, rf_outcome outcome, rf_event *event) {
    event->kind = RF_EV_OUTCOME;
    event->id = r->id;
    event->outcome = (uint8_t)outcome;
    event->number = r->replies;
    r->used = false;
}

/* Consumes the header of a frame that gives no event, and the rest of the frame as it comes. */
static rf_status drop(rf_peer *peer, const rf_header *header, size_t len, size_t *used) {
    size_t rest = header->key_len + header->body_len;
    size_t here = len - RF_HEADER_SIZE < rest ? len - RF_HEADER_SIZE : rest;
    *used = RF_HEADER_SIZE + here;
    peer->skip = rest - here;
    return RF_OK;
}

static rf_status read_reply(rf_peer *peer, const rf_header *header, const uint8_t *in, size_t len,
                            uint64_t now, size_t *used, rf_event *event) {
    request *r = find_request(peer, &header->id);
    /* Past the deadline nothing is delivered: rf_poll reports the outcome. */
    if (r == NULL || now >= r->deadline || header->argument != r->replies) {
        return drop(peer, header, len, used);
    }
    size_t total = (size_t)RF_HEADER_SIZE + header->body_len;
    if (len < total) {
        return RF_NEED_MORE;
    }
    event->kind = RF_EV_REPLY;
    event->id = r->id;
    event->flags = header->flags;
    event->number = r->replies++;
    event->body = (rf_bytes){.ptr = in + RF_HEADER_SIZE, .len = header->body_len};
    *used = total;
    return RF_OK;
}

static rf_status read_end(rf_peer *peer, const rf_header *header, uint64_t now, size_t *used,
                          rf_event *event) {
    *used = RF_HEADER_SIZE;
    request *r = find_request(peer, &header->id);
    if (r == NULL || now >= r->deadline) {
        return RF_OK;
    }
    if (header->flags == 0) {
        /* An end counts only when every reply it counts arrived (rule 5). */
        if (header->argument == r->replies) {
            finish(r, RF_COMPLETED, event);
        }
    } else if (r->sent_on == peer->connection && !r->marked) {
        /* A refusal answers only a copy of this connection that is not marked (rule 11). */
        finish(r, header->flags == RF_END_BUSY ? RF_BUSY : RF_UNROUTABLE, event);
    }
    return RF_OK;
}

static taken *find_taken(rf_peer *peer, const rf_id *id) {
    for (uint32_t i = 0; i < peer->config.max_ids; i++) {
        taken *t = &peer->ids[i];
        if (t->used && memcmp(t->id.bytes, id->bytes, sizeof id->bytes) == 0) {
            return t;
        }
    }
    return NULL;
}

static taken *free_taken(rf_peer *peer) {
    for (uint32_t i = 0; i < peer->config.max_ids; i++) {
        if (!peer->ids[i].used) {
            return &peer->ids[i];
        }
    }
    return NULL;
}

/* The deadline of a request on our clock (docs/wire-format.md, clocks and deadlines). */
static uint64_t local_deadline(const rf_peer *peer, uint64_t theirs) {
    if (theirs >= peer->their_clock) {
        return add(peer->read_at, theirs - peer->their_clock);
    }
    return since(peer->read_at, peer->their_clock - theirs);
}

static rf_status read_request(rf_peer *peer, const rf_header *header, const uint8_t *in,
                              size_t len, uint64_t now, size_t *used, rf_event *event) {
    if (len - RF_HEADER_SIZE < header->key_len) {
        return RF_NEED_MORE;
    }
    rf_bytes key = {.ptr = in + RF_HEADER_SIZE, .len = header->key_len};
    if (rf_key_check(key) != RF_OK) {
        return close_link(peer, RF_ERR_KEY, event);
    }
    uint64_t deadline = local_deadline(peer, header->argument);
    if (now >= deadline) {
        return drop(peer, header, len, used);
    }
    bool marked = (header->flags & RF_REQUEST_REPEAT) != 0;
    taken *known = find_taken(peer, &header->id);
    if (known != NULL) {
        /* A known id never runs again (rule 9). A connection carries one copy (rule 12), so
         * a second one asks for nothing. */
        if (marked && known->brought_on != peer->connection &&
            known->repeated_on != peer->connection) {
            known->repeated_on = peer->connection;
            event->kind = RF_EV_REPEAT;
            event->id = known->id;
            event->number = known->replies;
            event->ended = known->ended;
        }
        return drop(peer, header, len, used);
    }
    taken *slot = free_taken(peer);
    if (slot == NULL || deadline - now > peer->config.max_deadline_ms) {
        /* A copy marked as sent again is never refused (rule 11). */
        rf_header busy = {.kind = RF_END, .flags = RF_END_BUSY, .id = header->id};
        if (!marked && !queue(peer, &busy)) {
            return RF_WOULD_BLOCK;
        }
        return drop(peer, header, len, used);
    }
    size_t total = (size_t)RF_HEADER_SIZE + header->key_len + header->body_len;
    if (len < total) {
        return RF_NEED_MORE;
    }
    *slot = (taken){.id = header->id,
                    .deadline = deadline,
                    .brought_on = peer->connection,
                    .used = true,
                    .marked = marked};
    event->kind = RF_EV_REQUEST;
    event->id = header->id;
    event->flags = header->flags;
    event->deadline = deadline;
    event->key = key;
    event->body = (rf_bytes){.ptr = key.ptr + key.len, .len = header->body_len};
    *used = total;
    return RF_OK;
}

/* Writes the header of a frame of the replier side. */
static rf_status answer_header(const rf_peer *peer, uint8_t kind, uint8_t flags, rf_id id,
                               uint64_t argument, size_t body_len,
                               uint8_t header[RF_HEADER_SIZE]) {
    rf_header frame = {.kind = kind,
                       .flags = flags,
                       .id = id,
                       .argument = argument,
                       .body_len = body_len};
    return rf_header_encode(&frame, peer->config.max_length, header);
}

/* Whether a header of the caller would come before bytes of the core on the wire. */
static bool core_bytes_wait(const rf_peer *peer) {
    return peer->link == LINK_READY && peer->out_len > 0;
}

rf_status rf_reply(rf_peer *peer, rf_id id, size_t body_len, uint8_t flags,
                   uint8_t header[RF_HEADER_SIZE]) {
    taken *t = find_taken(peer, &id);
    if (t == NULL) {
        return RF_ERR_UNKNOWN_ID;
    }
    if (t->ended) {
        return RF_ERR_STATE;
    }
    if (core_bytes_wait(peer)) {
        return RF_WOULD_BLOCK;
    }
    rf_status status = answer_header(peer, RF_REPLY, flags, id, t->replies, body_len, header);
    if (status != RF_OK) {
        return status;
    }
    t->replies++;
    return peer->link == LINK_READY ? RF_OK : RF_OFFLINE;
}

rf_status rf_end(rf_peer *peer, rf_id id, uint8_t header[RF_HEADER_SIZE]) {
    taken *t = find_taken(peer, &id);
    if (t == NULL) {
        return RF_ERR_UNKNOWN_ID;
    }
    if (t->ended) {
        return RF_ERR_STATE;
    }
    if (core_bytes_wait(peer)) {
        return RF_WOULD_BLOCK;
    }
    rf_status status = answer_header(peer, RF_END, 0, id, t->replies, 0, header);
    if (status != RF_OK) {
        return status;
    }
    t->ended = true;
    return peer->link == LINK_READY ? RF_OK : RF_OFFLINE;
}

rf_status rf_refuse(rf_peer *peer, rf_id id, uint8_t end_flag, uint8_t header[RF_HEADER_SIZE]) {
    if (end_flag != RF_END_BUSY && end_flag != RF_END_NO_HANDLER) {
        return RF_ERR_FLAGS;
    }
    taken *t = find_taken(peer, &id);
    if (t == NULL) {
        return RF_ERR_UNKNOWN_ID;
    }
    if (t->ended || t->replies > 0) {
        return RF_ERR_STATE;
    }
    if (t->marked || t->brought_on != peer->connection || peer->link != LINK_READY) {
        t->used = false;
        return RF_DROPPED;
    }
    if (peer->out_len > 0) {
        return RF_WOULD_BLOCK;
    }
    rf_status status = answer_header(peer, RF_END, end_flag, id, 0, 0, header);
    if (status == RF_OK) {
        t->used = false;
    }
    return status;
}

/* Finds the taken request that stored frames can be sent again for. */
static rf_status again(rf_peer *peer, const rf_id *id, const taken **found) {
    if (peer->link != LINK_READY) {
        return RF_ERR_NOT_READY;
    }
    *found = find_taken(peer, id);
    if (*found == NULL) {
        return RF_ERR_UNKNOWN_ID;
    }
    return peer->out_len > 0 ? RF_WOULD_BLOCK : RF_OK;
}

rf_status rf_reply_again(rf_peer *peer, rf_id id, uint64_t number, size_t body_len,
                         uint8_t flags, uint8_t header[RF_HEADER_SIZE]) {
    const taken *t = NULL;
    rf_status status = again(peer, &id, &t);
    if (status != RF_OK) {
        return status;
    }
    if (number >= t->replies) {
        return RF_ERR_STATE;
    }
    return answer_header(peer, RF_REPLY, flags, id, number, body_len, header);
}

rf_status rf_end_again(rf_peer *peer, rf_id id, uint8_t header[RF_HEADER_SIZE]) {
    const taken *t = NULL;
    rf_status status = again(peer, &id, &t);
    if (status != RF_OK) {
        return status;
    }
    if (!t->ended) {
        return RF_ERR_STATE;
    }
    return answer_header(peer, RF_END, 0, id, t->replies, 0, header);
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
    if (header.kind == RF_REPLY) {
        return read_reply(peer, &header, in, len, now, used, event);
    }
    if (header.kind == RF_END) {
        return read_end(peer, &header, now, used, event);
    }
    return read_request(peer, &header, in, len, now, used, event);
}

/* Whether an open request waits for RF_EV_RESEND on this connection. */
static bool to_resend(const rf_peer *peer, const request *r) {
    return peer->link == LINK_READY && drops_repeats(peer) && r->used &&
           r->sent_on != peer->connection && r->asked_on != peer->connection;
}

static uint64_t forget_at(const rf_peer *peer, const taken *t) {
    return add(t->deadline, peer->config.keep_ms);
}

void rf_poll(rf_peer *peer, uint64_t now, rf_event *event) {
    *event = (rf_event){.kind = RF_EV_NONE};
    if (handshake_is_late(peer, now)) {
        (void)close_link(peer, RF_ERR_HANDSHAKE, event);
        return;
    }
    for (uint32_t i = 0; i < peer->config.max_requests; i++) {
        request *r = &peer->requests[i];
        if (r->used && now >= r->deadline) {
            finish(r, RF_EXPIRED, event);
            return;
        }
    }
    for (uint32_t i = 0; i < peer->config.max_requests; i++) {
        request *r = &peer->requests[i];
        if (to_resend(peer, r)) {
            r->asked_on = peer->connection;
            event->kind = RF_EV_RESEND;
            event->id = r->id;
            event->deadline = r->deadline;
            return;
        }
    }
    for (uint32_t i = 0; i < peer->config.max_ids; i++) {
        taken *t = &peer->ids[i];
        /* A handler that never ends keeps its id (rule 7). */
        if (t->used && t->ended && now >= forget_at(peer, t)) {
            t->used = false;
            event->kind = RF_EV_FORGOT;
            event->id = t->id;
            return;
        }
    }
}

static uint64_t earlier(uint64_t a, uint64_t b) { return a < b ? a : b; }

uint64_t rf_next_time(const rf_peer *peer) {
    uint64_t next = UINT64_MAX;
    if (peer->link == LINK_PREFACE || peer->link == LINK_CLOCK) {
        next = add(peer->connected_at, add(peer->config.handshake_ms, 1));
    }
    for (uint32_t i = 0; i < peer->config.max_requests; i++) {
        const request *r = &peer->requests[i];
        if (r->used) {
            next = earlier(next, to_resend(peer, r) ? 0 : r->deadline);
        }
    }
    for (uint32_t i = 0; i < peer->config.max_ids; i++) {
        const taken *t = &peer->ids[i];
        if (t->used && t->ended) {
            next = earlier(next, forget_at(peer, t));
        }
    }
    return next;
}
