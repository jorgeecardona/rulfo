/* The replier side of rf_peer, and two peers that talk. */
#include "wire.h"

/* The test sends each copy as a requester with the clock `clock` at the handshake. */
typedef struct {
    end b;
    uint8_t scratch[QUEUE];
    rf_event event;
} replier;

static void drain(rf_peer *peer) {
    uint8_t out[256];
    while (rf_output(peer, out, sizeof out) > 0) {
    }
}

/* Connects at `now` to a requester whose CLOCK says `clock`. */
static void greet(replier *r, uint64_t now, uint64_t clock) {
    uint8_t bytes[64];
    rf_preface_encode(RF_CAP_DROPS_REPEATS, bytes);
    rf_header header = {.kind = RF_CLOCK, .argument = clock};
    CHECK(rf_header_encode(&header, 0, bytes + RF_PREFACE_SIZE) == RF_OK);
    r->b.len = 0;
    rf_connected(r->b.peer, now);
    drain(r->b.peer);
    put(&r->b, bytes, RF_PREFACE_SIZE + RF_HEADER_SIZE);
    CHECK(step(&r->b, now, r->scratch, &r->event) == RF_OK && r->event.kind == RF_EV_READY);
    drain(r->b.peer);
}

static void start_with(replier *r, const rf_config *config) {
    open_end(&r->b, config);
    greet(r, 0, 0);
}

static void start(replier *r) {
    rf_config config;
    rf_config_default(&config);
    start_with(r, &config);
}

static rf_id id_of(uint8_t n) {
    rf_id id = {{0}};
    id.bytes[15] = n;
    return id;
}

/* Sends one copy of request `n` and lets the replier read it at `now`. */
static rf_status copy(replier *r, uint8_t n, uint64_t deadline, uint8_t flags, uint64_t now) {
    rf_frame frame = {.kind = RF_REQUEST,
                      .flags = flags,
                      .id = id_of(n),
                      .argument = deadline,
                      .key = {.ptr = (const uint8_t *)"op", .len = 2},
                      .body = {.ptr = (const uint8_t *)"in", .len = 2}};
    uint8_t out[64];
    size_t size = 0;
    CHECK(rf_frame_encode(&frame, 100, out, sizeof out, &size) == RF_OK);
    put(&r->b, out, size);
    return step(&r->b, now, r->scratch, &r->event);
}

static void expect_request(replier *r, uint8_t n, uint64_t deadline, uint8_t n_flags,
                           uint64_t now) {
    CHECK(copy(r, n, deadline, n_flags, now) == RF_OK);
    CHECK(r->event.kind == RF_EV_REQUEST && r->event.id.bytes[15] == n);
    CHECK(r->event.flags == n_flags);
    CHECK(r->event.key.len == 2 && memcmp(r->event.key.ptr, "op", 2) == 0);
    CHECK(r->event.body.len == 2 && memcmp(r->event.body.ptr, "in", 2) == 0);
    CHECK(r->b.len == 0);
}

static void expect_silence(replier *r, uint8_t n, uint64_t deadline, uint8_t flags,
                           uint64_t now) {
    CHECK(copy(r, n, deadline, flags, now) == RF_NEED_MORE);
    CHECK(r->event.kind == RF_EV_NONE && r->b.len == 0);
    uint8_t out[64];
    CHECK(rf_output(r->b.peer, out, sizeof out) == 0);
}

/* The core must have queued one busy end for request `n`. */
static void expect_busy(replier *r, uint8_t n) {
    uint8_t out[64];
    CHECK(rf_output(r->b.peer, out, sizeof out) == RF_HEADER_SIZE);
    rf_header header;
    CHECK(rf_header_decode(out, RF_HEADER_SIZE, 0, &header) == RF_OK);
    CHECK(header.kind == RF_END && header.flags == RF_END_BUSY);
    CHECK(header.id.bytes[15] == n && header.argument == 0);
}

static rf_header read_header(const uint8_t *bytes) {
    rf_header header;
    CHECK(rf_header_decode(bytes, RF_HEADER_SIZE, RF_DEFAULT_MAX_LENGTH, &header) == RF_OK);
    return header;
}

static void test_replies_are_numbered_and_the_end_counts_them(void) {
    replier r;
    start(&r);
    expect_request(&r, 1, 100, 0, 1);
    CHECK(r.event.deadline == 100);
    uint8_t h[RF_HEADER_SIZE];
    CHECK(rf_reply(r.b.peer, id_of(1), 3, 0, h) == RF_OK);
    CHECK(read_header(h).argument == 0 && read_header(h).body_len == 3);
    CHECK(rf_reply(r.b.peer, id_of(1), 0, RF_REPLY_ERROR, h) == RF_OK);
    CHECK(read_header(h).argument == 1 && read_header(h).flags == RF_REPLY_ERROR);
    CHECK(rf_reply(r.b.peer, id_of(1), 0, 0x80, h) == RF_ERR_FLAGS);
    CHECK(rf_end(r.b.peer, id_of(1), h) == RF_OK);
    CHECK(read_header(h).kind == RF_END && read_header(h).argument == 2);
    CHECK(rf_reply(r.b.peer, id_of(1), 0, 0, h) == RF_ERR_STATE);
    CHECK(rf_end(r.b.peer, id_of(1), h) == RF_ERR_STATE);
    CHECK(rf_refuse(r.b.peer, id_of(1), RF_END_BUSY, h) == RF_ERR_STATE);
    CHECK(rf_reply(r.b.peer, id_of(2), 0, 0, h) == RF_ERR_UNKNOWN_ID);
    CHECK(rf_end(r.b.peer, id_of(2), h) == RF_ERR_UNKNOWN_ID);
    close_end(&r.b);
}

static void test_a_known_id_never_runs_again(void) {
    replier r;
    start(&r);
    expect_request(&r, 1, 100, 0, 1);
    uint8_t h[RF_HEADER_SIZE];
    CHECK(rf_reply(r.b.peer, id_of(1), 0, 0, h) == RF_OK);
    /* A second copy on the same connection asks for nothing (rule 12). */
    expect_silence(&r, 1, 100, RF_REQUEST_REPEAT, 2);
    /* A marked copy on a new connection reports what was sent, once. */
    greet(&r, 2, 2);
    CHECK(copy(&r, 1, 100, RF_REQUEST_REPEAT, 2) == RF_OK);
    CHECK(r.event.kind == RF_EV_REPEAT && r.event.number == 1 && r.event.ended == 0);
    CHECK(r.event.id.bytes[15] == 1 && r.b.len == 0);
    expect_silence(&r, 1, 100, RF_REQUEST_REPEAT, 2);
    /* An unmarked one is dropped. */
    expect_silence(&r, 1, 100, 0, 2);
    CHECK(rf_end(r.b.peer, id_of(1), h) == RF_OK);
    greet(&r, 3, 3);
    CHECK(copy(&r, 1, 100, RF_REQUEST_REPEAT, 3) == RF_OK);
    CHECK(r.event.kind == RF_EV_REPEAT && r.event.number == 1 && r.event.ended != 0);

    CHECK(rf_reply_again(r.b.peer, id_of(1), 0, 5, 0, h) == RF_OK);
    CHECK(read_header(h).argument == 0 && read_header(h).body_len == 5);
    CHECK(rf_reply_again(r.b.peer, id_of(1), 1, 0, 0, h) == RF_ERR_STATE);
    CHECK(rf_end_again(r.b.peer, id_of(1), h) == RF_OK);
    CHECK(read_header(h).kind == RF_END && read_header(h).argument == 1);
    CHECK(rf_reply_again(r.b.peer, id_of(2), 0, 0, 0, h) == RF_ERR_UNKNOWN_ID);
    rf_disconnected(r.b.peer);
    CHECK(rf_reply_again(r.b.peer, id_of(1), 0, 0, 0, h) == RF_ERR_NOT_READY);
    CHECK(rf_end_again(r.b.peer, id_of(1), h) == RF_ERR_NOT_READY);
    close_end(&r.b);
}

static void test_replies_without_a_connection_are_counted(void) {
    replier r;
    start(&r);
    expect_request(&r, 1, 100, 0, 1);
    rf_disconnected(r.b.peer);
    uint8_t h[RF_HEADER_SIZE];
    CHECK(rf_end_again(r.b.peer, id_of(1), h) == RF_ERR_NOT_READY);
    CHECK(rf_reply(r.b.peer, id_of(1), 0, 0, h) == RF_OFFLINE);
    rf_connected(r.b.peer, 2); /* not ready yet */
    CHECK(rf_end(r.b.peer, id_of(1), h) == RF_OFFLINE);
    greet(&r, 3, 3);
    CHECK(copy(&r, 1, 100, RF_REQUEST_REPEAT, 4) == RF_OK);
    CHECK(r.event.kind == RF_EV_REPEAT && r.event.number == 1 && r.event.ended != 0);
    close_end(&r.b);
}

static void test_refusals(void) {
    replier r;
    start(&r);
    uint8_t h[RF_HEADER_SIZE];
    expect_request(&r, 1, 100, 0, 1);
    CHECK(rf_refuse(r.b.peer, id_of(1), 0, h) == RF_ERR_FLAGS);
    CHECK(rf_refuse(r.b.peer, id_of(1), RF_END_BUSY, h) == RF_OK);
    CHECK(read_header(h).flags == RF_END_BUSY && read_header(h).argument == 0);
    CHECK(rf_end(r.b.peer, id_of(1), h) == RF_ERR_UNKNOWN_ID);
    /* The id is forgotten: a retry runs. */
    expect_request(&r, 1, 100, 0, 2);
    CHECK(rf_refuse(r.b.peer, id_of(1), RF_END_NO_HANDLER, h) == RF_OK);

    /* A marked copy gets no refusal (rule 11). */
    memset(h, 0xEE, sizeof h);
    expect_request(&r, 2, 100, RF_REQUEST_REPEAT, 3);
    CHECK(rf_refuse(r.b.peer, id_of(2), RF_END_BUSY, h) == RF_DROPPED);
    CHECK(h[0] == 0xEE);
    CHECK(rf_end(r.b.peer, id_of(2), h) == RF_ERR_UNKNOWN_ID);

    /* The connection ended before the decision: nothing is sent, also on the next one. */
    expect_request(&r, 3, 100, 0, 4);
    greet(&r, 5, 5);
    CHECK(rf_refuse(r.b.peer, id_of(3), RF_END_BUSY, h) == RF_DROPPED);
    expect_request(&r, 3, 100, RF_REQUEST_REPEAT, 6);

    /* After a reply there is no refusal. */
    CHECK(rf_reply(r.b.peer, id_of(3), 0, 0, h) == RF_OK);
    CHECK(rf_refuse(r.b.peer, id_of(3), RF_END_BUSY, h) == RF_ERR_STATE);
    close_end(&r.b);
}

static void test_a_full_store_refuses(void) {
    rf_config config;
    rf_config_default(&config);
    config.max_ids = 1;
    config.max_out = 40;
    replier r;
    start_with(&r, &config);
    expect_request(&r, 1, 100, 0, 1);
    CHECK(copy(&r, 2, 100, 0, 1) == RF_NEED_MORE);
    CHECK(r.b.len == 0);

    /* The next busy end has no room until the first one is taken. */
    CHECK(copy(&r, 3, 100, 0, 1) == RF_WOULD_BLOCK);
    size_t waiting = r.b.len;
    CHECK(waiting > 0);
    uint8_t h[RF_HEADER_SIZE];
    CHECK(rf_reply(r.b.peer, id_of(1), 0, 0, h) == RF_WOULD_BLOCK);
    CHECK(rf_end(r.b.peer, id_of(1), h) == RF_WOULD_BLOCK);
    CHECK(rf_refuse(r.b.peer, id_of(1), RF_END_BUSY, h) == RF_WOULD_BLOCK);
    expect_busy(&r, 2);
    CHECK(step(&r.b, 1, r.scratch, &r.event) == RF_NEED_MORE && r.b.len == 0);
    expect_busy(&r, 3);
    /* A reply that was blocked was not counted. */
    CHECK(rf_end(r.b.peer, id_of(1), h) == RF_OK && read_header(h).argument == 0);

    /* A marked copy is dropped, not refused. */
    expect_silence(&r, 4, 100, RF_REQUEST_REPEAT, 2);
    close_end(&r.b);
}

static void test_a_far_deadline_is_refused(void) {
    rf_config config;
    rf_config_default(&config);
    config.max_deadline_ms = 50;
    replier r;
    start_with(&r, &config);
    expect_request(&r, 1, 51, 0, 1);
    CHECK(copy(&r, 2, 52, 0, 1) == RF_NEED_MORE);
    expect_busy(&r, 2);
    expect_silence(&r, 3, 52, RF_REQUEST_REPEAT, 1);
    close_end(&r.b);
}

static void test_the_deadline_is_converted(void) {
    replier r;
    start(&r);
    /* Our clock reads 1000 when their CLOCK says 50000. */
    greet(&r, 1000, 50000);
    expect_request(&r, 1, 50200, 0, 1199);
    CHECK(r.event.deadline == 1200);
    /* At the deadline the copy is dropped, however it is marked (rule 8). */
    expect_silence(&r, 2, 50200, 0, 1200);
    expect_silence(&r, 1, 50200, RF_REQUEST_REPEAT, 1200);
    /* Before their clock at the handshake. */
    expect_silence(&r, 3, 49000, 0, 1001);
    expect_silence(&r, 3, 0, 0, 0);
    /* A result below zero is zero, and one above the largest number is that number. */
    greet(&r, 10, UINT64_MAX);
    expect_silence(&r, 4, 0, 0, 0);
    rf_config config;
    rf_config_default(&config);
    greet(&r, UINT64_MAX - 100, 0);
    CHECK(copy(&r, 5, UINT64_MAX, 0, UINT64_MAX - 90) == RF_OK);
    CHECK(r.event.kind == RF_EV_REQUEST && r.event.deadline == UINT64_MAX);
    close_end(&r.b);
}

static void test_forgotten_after_the_keep_time(void) {
    replier r;
    start(&r);
    expect_request(&r, 1, 100, 0, 1);
    uint8_t h[RF_HEADER_SIZE];
    /* A handler that does not end keeps the id (rule 7). */
    rf_poll(r.b.peer, UINT64_MAX, &r.event);
    CHECK(r.event.kind == RF_EV_NONE);
    CHECK(rf_next_time(r.b.peer) == UINT64_MAX);
    CHECK(rf_end(r.b.peer, id_of(1), h) == RF_OK);
    CHECK(rf_next_time(r.b.peer) == 100 + 60000);
    rf_poll(r.b.peer, 100 + 60000 - 1, &r.event);
    CHECK(r.event.kind == RF_EV_NONE);
    rf_poll(r.b.peer, 100 + 60000, &r.event);
    CHECK(r.event.kind == RF_EV_FORGOT && r.event.id.bytes[15] == 1);
    CHECK(rf_end_again(r.b.peer, id_of(1), h) == RF_ERR_UNKNOWN_ID);
    close_end(&r.b);
}

/* Two processes send a copy each. Their clocks differ by at most `spread`, and each CLOCK
 * takes at most the handshake limit. Returns whether any case ran the request twice. */
static int runs_twice(uint32_t limit, uint32_t keep, uint64_t spread) {
    rf_config config;
    rf_config_default(&config);
    config.handshake_ms = limit;
    config.keep_ms = keep;
    config.max_ids = 2; /* a new peer for each case: keep it small */
    const uint64_t base = 100;
    const uint64_t deadline = base + 20;
    for (uint64_t off1 = 0; off1 <= spread; off1++) {
        for (uint64_t off2 = 0; off2 <= spread; off2++) {
            for (uint64_t delay1 = 0; delay1 <= limit; delay1++) {
                for (uint64_t delay2 = 0; delay2 <= limit; delay2++) {
                    for (uint64_t late = 0; late <= 60; late++) {
                        replier r;
                        start_with(&r, &config);
                        uint64_t now = base;
                        /* A sender with offset `off` shows `t + off` at the time t. */
                        rf_disconnected(r.b.peer);
                        greet(&r, now, now - delay1 + off1);
                        CHECK(copy(&r, 1, deadline, 0, now) != RF_WOULD_BLOCK);
                        if (r.event.kind != RF_EV_REQUEST) {
                            close_end(&r.b);
                            continue;
                        }
                        uint8_t h[RF_HEADER_SIZE];
                        CHECK(rf_end(r.b.peer, id_of(1), h) == RF_OK);
                        greet(&r, now + delay2, now + off2);
                        uint64_t read = now + delay2 + late;
                        rf_poll(r.b.peer, read, &r.event);
                        (void)copy(&r, 1, deadline, RF_REQUEST_REPEAT, read);
                        int twice = r.event.kind == RF_EV_REQUEST;
                        close_end(&r.b);
                        if (twice) {
                            return 1;
                        }
                    }
                }
            }
        }
    }
    return 0;
}

static void test_the_keep_time_covers_two_senders(void) {
    /* keep >= handshake limit + spread (rules 7 and 14). */
    CHECK(!runs_twice(4, 4, 0));
    CHECK(!runs_twice(4, 7, 3));
    CHECK(runs_twice(4, 4, 3));
}

/* Moves one header of `from`, with its key and body, to `to`. */
static void send(end *from, end *to, const uint8_t *header, const char *key, const char *body) {
    flush(from, to);
    put(to, header, RF_HEADER_SIZE);
    put(to, (const uint8_t *)key, strlen(key));
    put(to, (const uint8_t *)body, strlen(body));
}

static void test_two_peers_over_a_reconnect(void) {
    rf_config config;
    rf_config_default(&config);
    end a, b;
    open_end(&a, &config);
    open_end(&b, &config);
    /* The clocks have nothing in common. */
    join(&a, &b, 5000, 70);
    uint8_t scratch[QUEUE];
    uint8_t h[RF_HEADER_SIZE];
    rf_event event;
    rf_id id = id_of(9);
    rf_bytes key = {.ptr = (const uint8_t *)"roll", .len = 4};

    CHECK(rf_request(a.peer, id, key, 2, 5100, 0, 5000, h) == RF_OK);
    send(&a, &b, h, "roll", "v2");
    CHECK(step(&b, 71, scratch, &event) == RF_OK && event.kind == RF_EV_REQUEST);
    CHECK(event.deadline == 170);
    CHECK(rf_reply(b.peer, id, 5, 0, h) == RF_OK);
    send(&b, &a, h, "", "start");
    CHECK(step(&a, 5002, scratch, &event) == RF_OK && event.kind == RF_EV_REPLY);

    /* The second reply and the end are lost with the connection. */
    CHECK(rf_reply(b.peer, id, 4, 0, h) == RF_OK);
    CHECK(rf_end(b.peer, id, h) == RF_OK);
    rf_disconnected(a.peer);
    rf_disconnected(b.peer);
    join(&a, &b, 5010, 80);

    rf_poll(a.peer, 5010, &event);
    CHECK(event.kind == RF_EV_RESEND);
    CHECK(rf_request(a.peer, event.id, key, 2, event.deadline, 1, 5010, h) == RF_OK);
    send(&a, &b, h, "roll", "v2");
    CHECK(step(&b, 81, scratch, &event) == RF_OK && event.kind == RF_EV_REPEAT);
    CHECK(event.number == 2 && event.ended != 0);

    CHECK(rf_reply_again(b.peer, id, 0, 5, 0, h) == RF_OK);
    send(&b, &a, h, "", "start");
    CHECK(rf_reply_again(b.peer, id, 1, 4, 0, h) == RF_OK);
    send(&b, &a, h, "", "done");
    CHECK(rf_end_again(b.peer, id, h) == RF_OK);
    send(&b, &a, h, "", "");
    CHECK(step(&a, 5011, scratch, &event) == RF_OK && event.kind == RF_EV_REPLY);
    CHECK(event.number == 1 && memcmp(event.body.ptr, "done", 4) == 0);
    CHECK(step(&a, 5011, scratch, &event) == RF_OK && event.kind == RF_EV_OUTCOME);
    CHECK(event.outcome == RF_COMPLETED && event.number == 2);
    CHECK(a.len == 0);
    close_end(&a);
    close_end(&b);
}

/* The handler sends a reply and the end between RF_EV_REPEAT and the replay. */
static void test_a_replay_that_follows_the_header_comment(void) {
    rf_config config;
    rf_config_default(&config);
    end a, b;
    open_end(&a, &config);
    open_end(&b, &config);
    join(&a, &b, 0, 0);
    uint8_t scratch[QUEUE];
    uint8_t h[RF_HEADER_SIZE];
    rf_event event;
    rf_id id = id_of(3);
    rf_bytes key = {.ptr = (const uint8_t *)"op", .len = 2};
    CHECK(rf_request(a.peer, id, key, 0, 100, 0, 0, h) == RF_OK);
    send(&a, &b, h, "op", "");
    CHECK(step(&b, 1, scratch, &event) == RF_OK && event.kind == RF_EV_REQUEST);
    CHECK(rf_reply(b.peer, id, 0, 0, h) == RF_OK); /* lost with the connection */

    join(&a, &b, 2, 2);
    rf_poll(a.peer, 2, &event);
    CHECK(event.kind == RF_EV_RESEND);
    CHECK(rf_request(a.peer, id, key, 0, 100, 1, 2, h) == RF_OK);
    send(&a, &b, h, "op", "");
    CHECK(step(&b, 3, scratch, &event) == RF_OK && event.kind == RF_EV_REPEAT);
    CHECK(event.number == 1 && event.ended == 0);

    CHECK(rf_reply(b.peer, id, 0, 0, h) == RF_OK);
    send(&b, &a, h, "", "");
    CHECK(rf_end(b.peer, id, h) == RF_OK);
    send(&b, &a, h, "", "");

    uint64_t number = 0;
    while (rf_reply_again(b.peer, id, number, 0, 0, h) == RF_OK) {
        send(&b, &a, h, "", "");
        number++;
    }
    CHECK(number == 2);
    CHECK(rf_end_again(b.peer, id, h) == RF_OK);
    send(&b, &a, h, "", "");

    CHECK(step(&a, 4, scratch, &event) == RF_OK && event.kind == RF_EV_REPLY);
    CHECK(step(&a, 4, scratch, &event) == RF_OK && event.kind == RF_EV_REPLY);
    CHECK(step(&a, 4, scratch, &event) == RF_OK && event.kind == RF_EV_OUTCOME);
    CHECK(event.outcome == RF_COMPLETED && event.number == 2);
    close_end(&a);
    close_end(&b);
}

/* The sustained rate that the comment of max_ids gives. */
static void test_ids_fill_up_until_the_keep_time(void) {
    rf_config config;
    rf_config_default(&config);
    config.max_ids = 2;
    replier r;
    start_with(&r, &config);
    uint8_t h[RF_HEADER_SIZE];
    for (uint8_t n = 1; n <= 2; n++) {
        expect_request(&r, n, 100, 0, 1);
        CHECK(rf_end(r.b.peer, id_of(n), h) == RF_OK);
    }
    CHECK(copy(&r, 3, 60200, 0, 60099) == RF_NEED_MORE);
    expect_busy(&r, 3);
    rf_poll(r.b.peer, 60100, &r.event);
    CHECK(r.event.kind == RF_EV_FORGOT);
    expect_request(&r, 3, 60200, 0, 60100);
    close_end(&r.b);
}

int main(void) {
    test_replies_are_numbered_and_the_end_counts_them();
    test_a_known_id_never_runs_again();
    test_replies_without_a_connection_are_counted();
    test_refusals();
    test_a_full_store_refuses();
    test_a_far_deadline_is_refused();
    test_the_deadline_is_converted();
    test_forgotten_after_the_keep_time();
    test_the_keep_time_covers_two_senders();
    test_two_peers_over_a_reconnect();
    test_a_replay_that_follows_the_header_comment();
    test_ids_fill_up_until_the_keep_time();
    puts("replier: all tests passed");
    return 0;
}
