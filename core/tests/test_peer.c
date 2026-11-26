/* rf_peer against docs/operations.md and docs/wire-format.md. */
#include "wire.h"

#include <stdalign.h>

static void test_config_and_memory(void) {
    rf_config config;
    rf_config_default(&config);
    size_t size = rf_peer_size(&config);
    CHECK(size > config.max_out);

    rf_config bad = config;
    bad.max_out = 39;
    CHECK(rf_peer_size(&bad) == 0);
    bad = config;
    bad.handshake_ms = 0;
    CHECK(rf_peer_size(&bad) == 0);
    bad = config;
    bad.keep_ms = bad.handshake_ms - 1;
    CHECK(rf_peer_size(&bad) == 0);

    uint8_t *memory = malloc(size + 1);
    CHECK(memory != NULL);
    rf_peer *peer = NULL;
    CHECK(rf_peer_init(memory, size, &bad, &peer) == RF_ERR_CONFIG);
    CHECK(rf_peer_init(memory, size - 1, &config, &peer) == RF_ERR_MEMORY);
    CHECK(rf_peer_init(memory + 1, size, &config, &peer) == RF_ERR_MEMORY);
    CHECK(rf_peer_init(NULL, size, &config, &peer) == RF_ERR_MEMORY);
    CHECK(peer == NULL);
    CHECK(rf_peer_init(memory, size, &config, &peer) == RF_OK);
    CHECK(peer != NULL);
    free(memory);
}

static void test_no_connection(void) {
    rf_config config;
    rf_config_default(&config);
    end a;
    open_end(&a, &config);
    uint8_t byte = 0;
    size_t used = 1;
    rf_event event;
    CHECK(rf_input(a.peer, &byte, 1, 0, &used, &event) == RF_ERR_STATE);
    CHECK(used == 0 && event.kind == RF_EV_NONE);
    CHECK(rf_output(a.peer, &byte, 1) == 0);
    CHECK(rf_next_time(a.peer) == UINT64_MAX);
    close_end(&a);
}

static void test_handshake(void) {
    rf_config config;
    rf_config_default(&config);
    end a, b;
    open_end(&a, &config);
    open_end(&b, &config);

    rf_connected(a.peer, 100);
    CHECK(rf_next_time(a.peer) == 100 + 5000 + 1);
    uint8_t out[64];
    CHECK(rf_output(a.peer, out, sizeof out) == RF_PREFACE_SIZE);
    uint16_t capabilities = 0;
    CHECK(rf_preface_decode(out, RF_PREFACE_SIZE, &capabilities) == RF_OK);
    CHECK(capabilities == RF_CAP_DROPS_REPEATS);
    /* No CLOCK before the other preface arrived. */
    CHECK(rf_output(a.peer, out, sizeof out) == 0);

    join(&a, &b, 100, 900000);
    CHECK(rf_next_time(a.peer) == UINT64_MAX);
    close_end(&a);
    close_end(&b);
}

/* The bytes a peer with another clock sends first. */
static size_t greeting(uint8_t *out, uint64_t clock) {
    rf_preface_encode(RF_CAP_DROPS_REPEATS, out);
    rf_header header = {.kind = RF_CLOCK, .argument = clock};
    CHECK(rf_header_encode(&header, 0, out + RF_PREFACE_SIZE) == RF_OK);
    return RF_PREFACE_SIZE + RF_HEADER_SIZE;
}

static void test_every_split_of_the_greeting(void) {
    rf_config config;
    rf_config_default(&config);
    uint8_t bytes[64];
    size_t total = greeting(bytes, 7);
    for (size_t first = 0; first <= total; first++) {
        end a;
        open_end(&a, &config);
        rf_connected(a.peer, 0);
        uint8_t scratch[QUEUE];
        rf_event event;
        put(&a, bytes, first);
        rf_status status = step(&a, 1, scratch, &event);
        if (first < total) {
            CHECK(status == RF_NEED_MORE);
            put(&a, bytes + first, total - first);
            status = step(&a, 1, scratch, &event);
        }
        CHECK(status == RF_OK && event.kind == RF_EV_READY);
        CHECK(a.len == 0);
        close_end(&a);
    }
}

static void test_the_handshake_limit(void) {
    rf_config config;
    rf_config_default(&config);
    uint8_t bytes[64];
    size_t total = greeting(bytes, 7);
    uint8_t scratch[QUEUE];
    rf_event event;
    end a;
    open_end(&a, &config);

    /* At the limit the CLOCK is accepted. */
    rf_connected(a.peer, 1000);
    put(&a, bytes, total);
    CHECK(step(&a, 6000, scratch, &event) == RF_OK && event.kind == RF_EV_READY);

    rf_connected(a.peer, 1000);
    a.len = 0;
    put(&a, bytes, total);
    CHECK(step(&a, 6001, scratch, &event) == RF_ERR_HANDSHAKE);
    CHECK(event.kind == RF_EV_CLOSE && event.status == RF_ERR_HANDSHAKE);
    CHECK(step(&a, 6001, scratch, &event) == RF_ERR_STATE);

    /* Without any byte, rf_poll closes. */
    rf_connected(a.peer, 1000);
    rf_poll(a.peer, 6000, &event);
    CHECK(event.kind == RF_EV_NONE);
    rf_poll(a.peer, rf_next_time(a.peer), &event);
    CHECK(event.kind == RF_EV_CLOSE && event.status == RF_ERR_HANDSHAKE);
    rf_poll(a.peer, 9000, &event);
    CHECK(event.kind == RF_EV_NONE);

    /* A clock that went back does not count as a wait. */
    rf_connected(a.peer, 1000);
    rf_poll(a.peer, 0, &event);
    CHECK(event.kind == RF_EV_NONE);

    /* Near the end of the clock the next time does not wrap around. */
    rf_connected(a.peer, UINT64_MAX - 1);
    CHECK(rf_next_time(a.peer) == UINT64_MAX);
    close_end(&a);
}

static void expect_close(const uint8_t *bytes, size_t len, rf_status status) {
    rf_config config;
    rf_config_default(&config);
    uint8_t scratch[QUEUE];
    rf_event event;
    end a;
    open_end(&a, &config);
    rf_connected(a.peer, 0);
    put(&a, bytes, len);
    rf_status got = step(&a, 0, scratch, &event);
    if (got == RF_OK && event.kind == RF_EV_READY) {
        got = step(&a, 0, scratch, &event);
    }
    CHECK(got == status);
    CHECK(event.kind == RF_EV_CLOSE && event.status == status);
    close_end(&a);
}

static void test_protocol_errors(void) {
    uint8_t bytes[128];
    size_t total = greeting(bytes, 7);

    uint8_t wrong[128];
    memcpy(wrong, bytes, total);
    wrong[0] = 'X';
    expect_close(wrong, total, RF_ERR_MAGIC);

    /* A second CLOCK. */
    memcpy(bytes + total, bytes + RF_PREFACE_SIZE, RF_HEADER_SIZE);
    expect_close(bytes, total + RF_HEADER_SIZE, RF_ERR_HANDSHAKE);

    /* Another frame before the CLOCK. */
    rf_header end_frame = {.kind = RF_END};
    CHECK(rf_header_encode(&end_frame, 0, bytes + RF_PREFACE_SIZE) == RF_OK);
    expect_close(bytes, total, RF_ERR_HANDSHAKE);

    bytes[RF_PREFACE_SIZE + 4] = 0x7F;
    expect_close(bytes, total, RF_ERR_KIND);
}

static void test_small_output(void) {
    rf_config config;
    rf_config_default(&config);
    config.max_out = RF_PREFACE_SIZE + RF_HEADER_SIZE;
    end a, b;
    open_end(&a, &config);
    open_end(&b, &config);
    uint8_t bytes[64];
    size_t total = greeting(bytes, 7);
    uint8_t scratch[QUEUE];
    rf_event event;

    /* The preface still fills 8 of the 40 bytes, so the CLOCK fits exactly. */
    rf_connected(a.peer, 0);
    put(&a, bytes, total);
    CHECK(step(&a, 0, scratch, &event) == RF_OK && event.kind == RF_EV_READY);

    /* One byte at a time. */
    size_t n = 0;
    uint8_t out[64];
    while (rf_output(a.peer, out + n, 1) == 1) {
        n++;
    }
    CHECK(n == total);
    uint16_t capabilities;
    CHECK(rf_preface_decode(out, n, &capabilities) == RF_OK);
    rf_header header;
    CHECK(rf_header_decode(out + RF_PREFACE_SIZE, RF_HEADER_SIZE, 0, &header) == RF_OK);
    CHECK(header.kind == RF_CLOCK && header.argument == 0);
    close_end(&a);
    close_end(&b);
}

/* A requester joined to a replier that the test plays with the codec. */
typedef struct {
    end a;
    uint8_t scratch[QUEUE];
    rf_event event;
} scripted;

static void greet(scripted *s, uint64_t now, uint16_t capabilities) {
    uint8_t bytes[64];
    size_t total = greeting(bytes, 7);
    rf_preface_encode(capabilities, bytes);
    s->a.len = 0;
    rf_connected(s->a.peer, now);
    uint8_t out[64];
    while (rf_output(s->a.peer, out, sizeof out) > 0) {
    }
    put(&s->a, bytes, total);
    CHECK(step(&s->a, now, s->scratch, &s->event) == RF_OK && s->event.kind == RF_EV_READY);
    while (rf_output(s->a.peer, out, sizeof out) > 0) {
    }
}

static void start(scripted *s, uint32_t max_requests) {
    rf_config config;
    rf_config_default(&config);
    config.max_requests = max_requests;
    open_end(&s->a, &config);
    greet(s, 0, RF_CAP_DROPS_REPEATS);
}

static rf_id id_of(uint8_t n) {
    rf_id id = {{0}};
    id.bytes[15] = n;
    return id;
}

static const rf_bytes some_key = {.ptr = (const uint8_t *)"k", .len = 1};

static rf_status ask(scripted *s, uint8_t n, uint64_t deadline, int repeat, uint64_t now) {
    uint8_t header[RF_HEADER_SIZE];
    return rf_request(s->a.peer, id_of(n), some_key, 0, deadline, repeat, now, header);
}

/* The replier of the test sends a frame, and the requester reads it at `now`. */
static void answer(scripted *s, uint8_t kind, uint8_t flags, uint8_t n, uint64_t argument,
                   const char *body, uint64_t now) {
    rf_frame frame = {.kind = kind,
                      .flags = flags,
                      .id = id_of(n),
                      .argument = argument,
                      .body = {.ptr = (const uint8_t *)body, .len = strlen(body)}};
    uint8_t out[256];
    size_t size = 0;
    CHECK(rf_frame_encode(&frame, RF_DEFAULT_MAX_LENGTH, out, sizeof out, &size) == RF_OK);
    put(&s->a, out, size);
    rf_status status = step(&s->a, now, s->scratch, &s->event);
    CHECK(status == RF_OK || status == RF_NEED_MORE);
    CHECK(s->a.len == 0);
}

static void expect_outcome(const scripted *s, uint8_t n, rf_outcome outcome, uint64_t replies) {
    CHECK(s->event.kind == RF_EV_OUTCOME);
    CHECK(s->event.id.bytes[15] == n);
    CHECK(s->event.outcome == outcome && s->event.number == replies);
}

static void expect_nothing_due(scripted *s, uint64_t now) {
    rf_poll(s->a.peer, now, &s->event);
    CHECK(s->event.kind == RF_EV_NONE);
}

static void test_request_header(void) {
    scripted s;
    start(&s, 4);
    uint8_t header[RF_HEADER_SIZE];
    CHECK(rf_request(s.a.peer, id_of(1), some_key, 3, 50, 0, 10, header) == RF_OK);
    rf_header read;
    CHECK(rf_header_decode(header, sizeof header, 100, &read) == RF_OK);
    CHECK(read.kind == RF_REQUEST && read.flags == 0 && read.argument == 50);
    CHECK(read.key_len == 1 && read.body_len == 3 && read.id.bytes[15] == 1);
    CHECK(rf_next_time(s.a.peer) == 50);
    close_end(&s.a);
}

static void test_replies_in_order_then_completed(void) {
    scripted s;
    start(&s, 4);
    CHECK(ask(&s, 1, 100, 0, 0) == RF_OK);
    answer(&s, RF_REPLY, 0, 1, 0, "one", 1);
    CHECK(s.event.kind == RF_EV_REPLY && s.event.number == 0 && s.event.flags == 0);
    CHECK(s.event.body.len == 3 && memcmp(s.event.body.ptr, "one", 3) == 0);
    /* A number it already has, and one above the next, are dropped (rule 3). */
    answer(&s, RF_REPLY, 0, 1, 0, "one", 2);
    CHECK(s.event.kind == RF_EV_NONE);
    answer(&s, RF_REPLY, 0, 1, 2, "three", 2);
    CHECK(s.event.kind == RF_EV_NONE);
    /* An end that counts a reply that did not arrive is dropped (rule 5). */
    answer(&s, RF_END, 0, 1, 2, "", 3);
    CHECK(s.event.kind == RF_EV_NONE);
    answer(&s, RF_REPLY, RF_REPLY_ERROR, 1, 1, "two", 3);
    CHECK(s.event.kind == RF_EV_REPLY && s.event.number == 1);
    CHECK(s.event.flags == RF_REPLY_ERROR);
    answer(&s, RF_END, 0, 1, 2, "", 4);
    expect_outcome(&s, 1, RF_COMPLETED, 2);
    /* After the outcome nothing arrives, and there is no second outcome. */
    answer(&s, RF_REPLY, 0, 1, 2, "late", 5);
    CHECK(s.event.kind == RF_EV_NONE);
    answer(&s, RF_END, 0, 1, 2, "", 5);
    CHECK(s.event.kind == RF_EV_NONE);
    expect_nothing_due(&s, 1000);
    close_end(&s.a);
}

static void test_a_reply_in_pieces(void) {
    scripted s;
    start(&s, 4);
    CHECK(ask(&s, 1, 100, 0, 0) == RF_OK);
    rf_frame frame = {.kind = RF_REPLY,
                      .id = id_of(1),
                      .body = {.ptr = (const uint8_t *)"abc", .len = 3}};
    uint8_t out[64];
    size_t size = 0;
    CHECK(rf_frame_encode(&frame, 3, out, sizeof out, &size) == RF_OK);
    for (size_t i = 0; i + 1 < size; i++) {
        put(&s.a, out + i, 1);
        CHECK(step(&s.a, 1, s.scratch, &s.event) == RF_NEED_MORE);
    }
    put(&s.a, out + size - 1, 1);
    CHECK(step(&s.a, 1, s.scratch, &s.event) == RF_OK);
    CHECK(s.event.kind == RF_EV_REPLY && memcmp(s.event.body.ptr, "abc", 3) == 0);

    /* A dropped reply is not buffered: each piece is consumed as it comes. */
    for (size_t i = 0; i < size; i++) {
        put(&s.a, out + i, 1);
        CHECK(step(&s.a, 1, s.scratch, &s.event) == RF_NEED_MORE);
        CHECK(s.a.len == (i < RF_HEADER_SIZE - 1 ? i + 1 : 0));
    }
    answer(&s, RF_END, 0, 1, 1, "", 2);
    expect_outcome(&s, 1, RF_COMPLETED, 1);
    close_end(&s.a);
}

static void test_refusals(void) {
    scripted s;
    start(&s, 4);
    CHECK(ask(&s, 1, 100, 0, 0) == RF_OK);
    answer(&s, RF_END, RF_END_BUSY, 1, 0, "", 1);
    expect_outcome(&s, 1, RF_BUSY, 0);
    /* Rule 13: the same id and deadline again, as a new copy. */
    CHECK(ask(&s, 1, 100, 0, 2) == RF_OK);
    answer(&s, RF_END, RF_END_NO_HANDLER, 1, 0, "", 3);
    expect_outcome(&s, 1, RF_UNROUTABLE, 0);
    close_end(&s.a);
}

static void test_expired(void) {
    scripted s;
    start(&s, 4);
    CHECK(ask(&s, 1, 100, 0, 100) == RF_ERR_EXPIRED);
    CHECK(ask(&s, 1, 100, 0, 99) == RF_OK);
    expect_nothing_due(&s, 99);
    /* At the deadline an end is too late, and rf_poll gives the one outcome. */
    answer(&s, RF_END, 0, 1, 0, "", 100);
    CHECK(s.event.kind == RF_EV_NONE);
    answer(&s, RF_REPLY, 0, 1, 0, "x", 100);
    CHECK(s.event.kind == RF_EV_NONE);
    rf_poll(s.a.peer, 100, &s.event);
    expect_outcome(&s, 1, RF_EXPIRED, 0);
    expect_nothing_due(&s, 200);
    /* Also without a connection. */
    CHECK(ask(&s, 2, 300, 0, 200) == RF_OK);
    rf_disconnected(s.a.peer);
    CHECK(rf_next_time(s.a.peer) == 300);
    rf_poll(s.a.peer, 300, &s.event);
    expect_outcome(&s, 2, RF_EXPIRED, 0);
    close_end(&s.a);
}

static void test_calls_that_are_refused(void) {
    scripted s;
    start(&s, 1);
    uint8_t header[RF_HEADER_SIZE];
    memset(header, 0xEE, sizeof header);
    rf_bytes empty = {.ptr = NULL, .len = 0};
    rf_bytes bad = {.ptr = (const uint8_t *)"\xFF", .len = 1};
    CHECK(rf_request(s.a.peer, id_of(1), empty, 0, 100, 0, 0, header) == RF_ERR_KEY);
    CHECK(rf_request(s.a.peer, id_of(1), bad, 0, 100, 0, 0, header) == RF_ERR_KEY);
    CHECK(rf_request(s.a.peer, id_of(1), some_key, RF_DEFAULT_MAX_LENGTH, 100, 0, 0, header) ==
          RF_ERR_LENGTH);
    CHECK(header[0] == 0xEE && header[RF_HEADER_SIZE - 1] == 0xEE);
    CHECK(rf_next_time(s.a.peer) == UINT64_MAX); /* nothing was opened */

    CHECK(ask(&s, 1, 100, 0, 0) == RF_OK);
    CHECK(ask(&s, 2, 100, 0, 0) == RF_ERR_FULL);
    /* Rule 12: one copy without an end per connection. */
    CHECK(ask(&s, 1, 100, 0, 0) == RF_ERR_STATE);
    CHECK(ask(&s, 1, 100, 1, 0) == RF_ERR_STATE);

    rf_disconnected(s.a.peer);
    CHECK(ask(&s, 1, 100, 1, 0) == RF_ERR_NOT_READY);
    rf_connected(s.a.peer, 0);
    CHECK(ask(&s, 1, 100, 1, 0) == RF_ERR_NOT_READY);
    close_end(&s.a);
}

static void test_core_bytes_go_first(void) {
    scripted s;
    start(&s, 4);
    uint8_t bytes[64];
    size_t total = greeting(bytes, 7);
    rf_connected(s.a.peer, 0);
    s.a.len = 0;
    put(&s.a, bytes, total);
    CHECK(step(&s.a, 0, s.scratch, &s.event) == RF_OK && s.event.kind == RF_EV_READY);
    /* The preface and the CLOCK still wait in the core. */
    CHECK(ask(&s, 1, 100, 0, 0) == RF_WOULD_BLOCK);
    uint8_t out[64];
    CHECK(rf_output(s.a.peer, out, sizeof out) == RF_PREFACE_SIZE + RF_HEADER_SIZE);
    CHECK(ask(&s, 1, 100, 0, 0) == RF_OK);
    close_end(&s.a);
}

static void test_resend_after_a_reconnect(void) {
    scripted s;
    start(&s, 4);
    CHECK(ask(&s, 1, 100, 0, 0) == RF_OK);
    answer(&s, RF_REPLY, 0, 1, 0, "one", 1);
    expect_nothing_due(&s, 1);
    rf_disconnected(s.a.peer);
    expect_nothing_due(&s, 2);

    greet(&s, 10, RF_CAP_DROPS_REPEATS);
    CHECK(rf_next_time(s.a.peer) == 0);
    rf_poll(s.a.peer, 10, &s.event);
    CHECK(s.event.kind == RF_EV_RESEND && s.event.id.bytes[15] == 1);
    CHECK(s.event.deadline == 100);
    expect_nothing_due(&s, 10); /* once for each connection */
    CHECK(rf_next_time(s.a.peer) == 100);

    /* Before the new copy is sent, a refusal answers nothing (rules 11 and 12). */
    answer(&s, RF_END, RF_END_BUSY, 1, 0, "", 10);
    CHECK(s.event.kind == RF_EV_NONE);
    CHECK(ask(&s, 1, 100, 0, 10) == RF_ERR_STATE); /* repeat must be set */
    CHECK(ask(&s, 1, 101, 1, 10) == RF_ERR_STATE); /* the same deadline */
    uint8_t header[RF_HEADER_SIZE];
    CHECK(rf_request(s.a.peer, id_of(1), some_key, 0, 100, 1, 10, header) == RF_OK);
    CHECK(header[5] == RF_REQUEST_REPEAT);
    /* A copy marked as sent again is never answered with a refusal. */
    answer(&s, RF_END, RF_END_BUSY, 1, 0, "", 11);
    CHECK(s.event.kind == RF_EV_NONE);
    answer(&s, RF_END, RF_END_NO_HANDLER, 1, 0, "", 11);
    CHECK(s.event.kind == RF_EV_NONE);
    /* The replies sent again count once, and the numbering goes on. */
    answer(&s, RF_REPLY, 0, 1, 0, "one", 12);
    CHECK(s.event.kind == RF_EV_NONE);
    answer(&s, RF_REPLY, 0, 1, 1, "two", 12);
    CHECK(s.event.kind == RF_EV_REPLY && s.event.number == 1);
    answer(&s, RF_END, 0, 1, 2, "", 13);
    expect_outcome(&s, 1, RF_COMPLETED, 2);
    close_end(&s.a);
}

static void test_no_resend_to_a_peer_that_runs_repeats(void) {
    scripted s;
    start(&s, 4);
    greet(&s, 0, 0);
    CHECK(ask(&s, 1, 100, 1, 0) == RF_ERR_STATE);
    CHECK(ask(&s, 1, 100, 0, 0) == RF_OK);
    greet(&s, 1, 0);
    expect_nothing_due(&s, 1);
    CHECK(ask(&s, 1, 100, 1, 1) == RF_ERR_STATE);
    CHECK(rf_next_time(s.a.peer) == 100);
    close_end(&s.a);
}

static void test_another_process_sent_the_first_copy(void) {
    scripted s;
    start(&s, 4);
    CHECK(ask(&s, 1, 100, 1, 0) == RF_OK);
    answer(&s, RF_END, 0, 1, 0, "", 1);
    expect_outcome(&s, 1, RF_COMPLETED, 0);
    close_end(&s.a);
}

int main(void) {
    test_config_and_memory();
    test_no_connection();
    test_handshake();
    test_every_split_of_the_greeting();
    test_the_handshake_limit();
    test_protocol_errors();
    test_small_output();
    test_request_header();
    test_replies_in_order_then_completed();
    test_a_reply_in_pieces();
    test_refusals();
    test_expired();
    test_calls_that_are_refused();
    test_core_bytes_go_first();
    test_resend_after_a_reconnect();
    test_no_resend_to_a_peer_that_runs_repeats();
    test_another_process_sent_the_first_copy();
    puts("peer: all tests passed");
    return 0;
}
