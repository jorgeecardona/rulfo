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

int main(void) {
    test_config_and_memory();
    test_no_connection();
    test_handshake();
    test_every_split_of_the_greeting();
    test_the_handshake_limit();
    test_protocol_errors();
    test_small_output();
    puts("peer: all tests passed");
    return 0;
}
