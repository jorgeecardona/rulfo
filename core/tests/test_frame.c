/* The frame codec against the bytes and rules of docs/wire-format.md. */
#include "rulfo.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(condition)                                                       \
    do {                                                                       \
        if (!(condition)) {                                                    \
            fprintf(stderr, "%s:%d: failed: %s\n", __FILE__, __LINE__, #condition); \
            exit(1);                                                           \
        }                                                                      \
    } while (0)

#define MAX RF_DEFAULT_MAX_LENGTH

/* A REQUEST with key "ab", body "xyz", sent again, deadline 0x0102030405060708 ms. */
static const uint8_t request_bytes[] = {
    5,    0,    0,    0,                            /* length */
    0x01, 0x01,                                     /* kind, flags */
    2,    0,                                        /* key length */
    0,    1,    2,    3,    4,    5,    6,    7,    /* id */
    8,    9,    10,   11,   12,   13,   14,   15,   /* id */
    0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01, /* argument */
    'a',  'b',  'x',  'y',  'z'};

static rf_frame request_frame(void) {
    rf_frame frame = {.kind = RF_REQUEST,
                      .flags = RF_REQUEST_REPEAT,
                      .argument = 0x0102030405060708u,
                      .key = {.ptr = (const uint8_t *)"ab", .len = 2},
                      .body = {.ptr = (const uint8_t *)"xyz", .len = 3}};
    for (uint8_t i = 0; i < 16; i++) {
        frame.id.bytes[i] = i;
    }
    return frame;
}

static void test_preface(void) {
    const uint8_t expected[RF_PREFACE_SIZE] = {'R', 'U', 'L', 'F', 1, 0, 0x01, 0x80};
    uint8_t out[RF_PREFACE_SIZE];
    rf_preface_encode(0x8001, out);
    CHECK(memcmp(out, expected, sizeof out) == 0);

    uint16_t capabilities = 0;
    CHECK(rf_preface_decode(expected, sizeof expected, &capabilities) == RF_OK);
    CHECK(capabilities == 0x8001); /* unknown bits are not an error */

    for (size_t len = 0; len < sizeof expected; len++) {
        CHECK(rf_preface_decode(expected, len, &capabilities) == RF_NEED_MORE);
    }

    const struct {
        size_t offset;
        rf_status status;
    } bad[] = {{0, RF_ERR_MAGIC}, {3, RF_ERR_MAGIC}, {4, RF_ERR_VERSION}, {5, RF_ERR_RESERVED}};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        uint8_t changed[RF_PREFACE_SIZE];
        memcpy(changed, expected, sizeof changed);
        changed[bad[i].offset] ^= 0x40;
        capabilities = 7;
        CHECK(rf_preface_decode(changed, sizeof changed, &capabilities) == bad[i].status);
        CHECK(capabilities == 7);
    }
}

static void test_known_bytes(void) {
    rf_frame frame = request_frame();
    uint8_t out[64];
    size_t size = 0;
    CHECK(rf_frame_encode(&frame, MAX, out, sizeof out, &size) == RF_OK);
    CHECK(size == sizeof request_bytes);
    CHECK(memcmp(out, request_bytes, size) == 0);

    rf_frame read;
    CHECK(rf_frame_decode(request_bytes, sizeof request_bytes, MAX, &read, &size) == RF_OK);
    CHECK(size == sizeof request_bytes);
    CHECK(read.kind == frame.kind && read.flags == frame.flags);
    CHECK(memcmp(read.id.bytes, frame.id.bytes, 16) == 0);
    CHECK(read.argument == frame.argument);
    CHECK(read.key.len == 2 && memcmp(read.key.ptr, "ab", 2) == 0);
    CHECK(read.body.len == 3 && memcmp(read.body.ptr, "xyz", 3) == 0);
}

static void test_every_prefix_needs_more(void) {
    for (size_t len = 0; len < sizeof request_bytes; len++) {
        /* A buffer of exactly len bytes, so the sanitizer sees any read past it. */
        uint8_t *prefix = malloc(len == 0 ? 1 : len);
        CHECK(prefix != NULL);
        memcpy(prefix, request_bytes, len);
        rf_frame frame = {.kind = 99};
        size_t size = 0;
        CHECK(rf_frame_decode(prefix, len, MAX, &frame, &size) == RF_NEED_MORE);
        CHECK(size == (len < RF_HEADER_SIZE ? RF_HEADER_SIZE : sizeof request_bytes));
        CHECK(frame.kind == 99);
        free(prefix);
    }
}

static void test_two_frames_in_one_buffer(void) {
    uint8_t two[2 * sizeof request_bytes];
    memcpy(two, request_bytes, sizeof request_bytes);
    memcpy(two + sizeof request_bytes, request_bytes, sizeof request_bytes);
    rf_frame frame;
    size_t size = 0;
    CHECK(rf_frame_decode(two, sizeof two, MAX, &frame, &size) == RF_OK);
    CHECK(size == sizeof request_bytes);
}

/* The status of a header with the given fields and `length` zero bytes after it. */
static rf_status decode_header(uint8_t kind, uint8_t flags, uint8_t length, uint8_t key_len,
                               uint8_t argument) {
    uint8_t in[RF_HEADER_SIZE + 255] = {0};
    in[0] = length;
    in[4] = kind;
    in[5] = flags;
    in[6] = key_len;
    in[24] = argument;
    rf_frame frame;
    size_t size = 0;
    return rf_frame_decode(in, RF_HEADER_SIZE + (size_t)length, MAX, &frame, &size);
}

/* The status of encoding the same fields. */
static rf_status encode_header(uint8_t kind, uint8_t flags, size_t key_len, uint8_t argument) {
    static const uint8_t zeros[255] = {0};
    rf_frame frame = {.kind = kind,
                      .flags = flags,
                      .argument = argument,
                      .key = {.ptr = zeros, .len = key_len}};
    uint8_t out[RF_HEADER_SIZE + 255];
    size_t size = 0;
    return rf_frame_encode(&frame, MAX, out, sizeof out, &size);
}

static void test_header_rules(void) {
    const struct {
        uint8_t kind, flags, key_len, argument;
        rf_status status;
    } cases[] = {
        {RF_REQUEST, 0, 1, 9, RF_OK},
        {RF_REQUEST, 0, 0, 0, RF_ERR_KEY},
        {RF_REQUEST, RF_REQUEST_REPEAT, 1, 9, RF_OK},
        {RF_REQUEST, 0x02, 1, 9, RF_ERR_FLAGS},
        {RF_REPLY, 0, 0, 9, RF_OK},
        {RF_REPLY, RF_REPLY_ERROR, 0, 9, RF_OK},
        {RF_REPLY, 0x02, 0, 0, RF_ERR_FLAGS},
        {RF_REPLY, 0, 1, 0, RF_ERR_KEY},
        {RF_END, 0, 0, 9, RF_OK},
        {RF_END, RF_END_NO_HANDLER, 0, 0, RF_OK},
        {RF_END, RF_END_BUSY, 0, 0, RF_OK},
        {RF_END, RF_END_NO_HANDLER | RF_END_BUSY, 0, 0, RF_ERR_FLAGS},
        {RF_END, 0x04, 0, 0, RF_ERR_FLAGS},
        {RF_END, 0x80, 0, 0, RF_ERR_FLAGS},
        {RF_END, RF_END_NO_HANDLER, 0, 1, RF_ERR_ARGUMENT},
        {RF_END, RF_END_BUSY, 0, 1, RF_ERR_ARGUMENT},
        {RF_END, 0, 1, 0, RF_ERR_KEY},
        {0x00, 0, 0, 0, RF_ERR_KIND},
        {RF_CLOCK, 0, 0, 9, RF_OK},
        {RF_CLOCK, 0x01, 0, 0, RF_ERR_FLAGS},
        {RF_CLOCK, 0, 1, 0, RF_ERR_KEY},
        {0x05, 0, 0, 0, RF_ERR_KIND},
        {0x10, 0, 0, 0, RF_ERR_KIND}, /* reserved */
        {0xFF, 0, 0, 0, RF_ERR_KIND},
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        CHECK(decode_header(cases[i].kind, cases[i].flags, cases[i].key_len, cases[i].key_len,
                            cases[i].argument) == cases[i].status);
        CHECK(encode_header(cases[i].kind, cases[i].flags, cases[i].key_len, cases[i].argument) ==
              cases[i].status);
    }
    CHECK(decode_header(RF_REQUEST, 0, 1, 2, 0) == RF_ERR_KEY_LENGTH);
}

static void test_clock_has_a_zero_id_and_no_body(void) {
    rf_frame frame = {.kind = RF_CLOCK, .argument = 0x0102};
    uint8_t out[64];
    size_t size = 0;
    CHECK(rf_frame_encode(&frame, MAX, out, sizeof out, &size) == RF_OK);
    for (size_t i = 0; i < 16; i++) {
        CHECK(out[8 + i] == 0);
        rf_frame read;
        out[8 + i] = 1;
        CHECK(rf_frame_decode(out, size, MAX, &read, &size) == RF_ERR_ID);
        out[8 + i] = 0;
        frame.id.bytes[i] = 1;
        uint8_t other[64];
        CHECK(rf_frame_encode(&frame, MAX, other, sizeof other, &size) == RF_ERR_ID);
        frame.id.bytes[i] = 0;
        size = RF_HEADER_SIZE;
    }
    frame.body = (rf_bytes){.ptr = (const uint8_t *)"x", .len = 1};
    CHECK(rf_frame_encode(&frame, MAX, out, sizeof out, &size) == RF_ERR_BODY);
}

static void test_header_alone(void) {
    /* The header decodes without a byte of the key or the body. */
    uint8_t *only = malloc(RF_HEADER_SIZE);
    CHECK(only != NULL);
    memcpy(only, request_bytes, RF_HEADER_SIZE);
    rf_header header = {.kind = 99};
    for (size_t len = 0; len < RF_HEADER_SIZE; len++) {
        CHECK(rf_header_decode(only, len, MAX, &header) == RF_NEED_MORE);
        CHECK(header.kind == 99);
    }
    CHECK(rf_header_decode(only, RF_HEADER_SIZE, MAX, &header) == RF_OK);
    CHECK(header.kind == RF_REQUEST && header.flags == RF_REQUEST_REPEAT);
    CHECK(header.key_len == 2 && header.body_len == 3);
    CHECK(header.argument == 0x0102030405060708u);

    uint8_t out[RF_HEADER_SIZE];
    CHECK(rf_header_encode(&header, MAX, out) == RF_OK);
    CHECK(memcmp(out, request_bytes, sizeof out) == 0);

    memset(out, 0xEE, sizeof out);
    header.flags = 0x80;
    CHECK(rf_header_encode(&header, MAX, out) == RF_ERR_FLAGS);
    CHECK(out[0] == 0xEE && out[RF_HEADER_SIZE - 1] == 0xEE);

    header.kind = 99;
    CHECK(rf_header_decode(only, 4, 4, &header) == RF_ERR_LENGTH);
    only[4] = 0x7F;
    CHECK(rf_header_decode(only, RF_HEADER_SIZE, MAX, &header) == RF_ERR_KIND);
    CHECK(header.kind == 99);
    free(only);
}

static void test_key_check(void) {
    CHECK(rf_key_check((rf_bytes){.ptr = NULL, .len = 0}) == RF_OK);
    CHECK(rf_key_check((rf_bytes){.ptr = (const uint8_t *)"a/b", .len = 3}) == RF_OK);
    CHECK(rf_key_check((rf_bytes){.ptr = (const uint8_t *)"\xC2", .len = 1}) == RF_ERR_KEY);
}

static void test_bad_header_is_refused_before_the_body(void) {
    uint8_t in[RF_HEADER_SIZE] = {0};
    in[0] = 200;
    in[4] = 0x7F;
    rf_frame frame;
    size_t size = 0;
    CHECK(rf_frame_decode(in, sizeof in, MAX, &frame, &size) == RF_ERR_KIND);
}

static void test_end_has_no_body(void) {
    const uint8_t flags[] = {0, RF_END_NO_HANDLER, RF_END_BUSY};
    for (size_t i = 0; i < sizeof flags; i++) {
        rf_frame frame = {.kind = RF_END,
                          .flags = flags[i],
                          .body = {.ptr = (const uint8_t *)"x", .len = 1}};
        uint8_t out[64];
        size_t size = 0;
        CHECK(rf_frame_encode(&frame, MAX, out, sizeof out, &size) == RF_ERR_BODY);

        uint8_t in[RF_HEADER_SIZE] = {0};
        in[0] = 1;
        in[4] = RF_END;
        in[5] = flags[i];
        CHECK(rf_frame_decode(in, sizeof in, MAX, &frame, &size) == RF_ERR_BODY);
    }
}

static void test_bad_key_is_refused_before_the_body(void) {
    uint8_t in[RF_HEADER_SIZE + 2] = {0};
    in[0] = 200;
    in[4] = RF_REQUEST;
    in[6] = 2;
    in[RF_HEADER_SIZE] = 0xFF;
    in[RF_HEADER_SIZE + 1] = 0xFF;
    rf_frame frame = {.kind = 99};
    size_t size = 7;
    CHECK(rf_frame_decode(in, sizeof in - 1, MAX, &frame, &size) == RF_NEED_MORE);
    size = 7;
    CHECK(rf_frame_decode(in, sizeof in, MAX, &frame, &size) == RF_ERR_KEY);
    CHECK(size == 7 && frame.kind == 99);
}

static void test_longest_length(void) {
    rf_frame frame = request_frame(); /* key plus body is 5 */
    uint8_t out[64];
    size_t size = 0;
    CHECK(rf_frame_encode(&frame, 5, out, sizeof out, &size) == RF_OK);
    CHECK(rf_frame_encode(&frame, 4, out, sizeof out, &size) == RF_ERR_LENGTH);
    CHECK(rf_frame_encode(&frame, 1, out, sizeof out, &size) == RF_ERR_LENGTH);

    rf_frame read;
    CHECK(rf_frame_decode(request_bytes, sizeof request_bytes, 5, &read, &size) == RF_OK);
    /* Refused from the first 4 bytes: no buffer for the frame is needed. */
    CHECK(rf_frame_decode(request_bytes, 4, 4, &read, &size) == RF_ERR_LENGTH);

    const uint8_t huge[4] = {0xFF, 0xFF, 0xFF, 0xFF};
    CHECK(rf_frame_decode(huge, sizeof huge, MAX, &read, &size) == RF_ERR_LENGTH);

    /* A body length that would wrap a sum around. */
    frame.body.len = SIZE_MAX - 1;
    CHECK(rf_frame_encode(&frame, UINT32_MAX, out, sizeof out, &size) == RF_ERR_LENGTH);

    frame = request_frame();
    frame.key.len = (size_t)UINT16_MAX + 1;
    CHECK(rf_frame_encode(&frame, MAX, out, sizeof out, &size) == RF_ERR_KEY_LENGTH);
}

static void test_longest_key(void) {
    static uint8_t key[UINT16_MAX];
    static uint8_t out[RF_HEADER_SIZE + UINT16_MAX];
    memset(key, 'k', sizeof key);
    rf_frame frame = {.kind = RF_REQUEST, .key = {.ptr = key, .len = sizeof key}};
    size_t size = 0;
    CHECK(rf_frame_encode(&frame, MAX, out, sizeof out, &size) == RF_OK);
    rf_frame read;
    CHECK(rf_frame_decode(out, size, MAX, &read, &size) == RF_OK);
    CHECK(read.key.len == UINT16_MAX && read.body.len == 0);
}

static void test_small_output_buffer(void) {
    rf_frame frame = request_frame();
    for (size_t cap = 0; cap < sizeof request_bytes; cap++) {
        uint8_t *out = malloc(cap == 0 ? 1 : cap);
        CHECK(out != NULL);
        memset(out, 0xEE, cap);
        size_t size = 0;
        CHECK(rf_frame_encode(&frame, MAX, out, cap, &size) == RF_ERR_SPACE);
        CHECK(size == sizeof request_bytes);
        for (size_t i = 0; i < cap; i++) {
            CHECK(out[i] == 0xEE);
        }
        free(out);
    }
}

static void test_empty_key_and_body_may_be_null(void) {
    rf_frame frame = {.kind = RF_END, .argument = 3};
    uint8_t out[RF_HEADER_SIZE];
    size_t size = 0;
    CHECK(rf_frame_encode(&frame, 0, out, sizeof out, &size) == RF_OK);
    CHECK(size == RF_HEADER_SIZE);
}

static void check_key(const char *key, size_t len, rf_status status) {
    uint8_t out[64];
    size_t size = 0;
    rf_frame frame = {.kind = RF_REQUEST, .key = {.ptr = (const uint8_t *)key, .len = len}};
    CHECK(rf_frame_encode(&frame, MAX, out, sizeof out, &size) == status);

    uint8_t in[64] = {0};
    in[0] = (uint8_t)len;
    in[4] = RF_REQUEST;
    in[6] = (uint8_t)len;
    memcpy(in + RF_HEADER_SIZE, key, len);
    rf_frame read;
    CHECK(rf_frame_decode(in, RF_HEADER_SIZE + len, MAX, &read, &size) == status);
}

#define GOOD(literal) check_key(literal, sizeof literal - 1, RF_OK)
#define BAD(literal) check_key(literal, sizeof literal - 1, RF_ERR_KEY)

static void test_utf8_keys(void) {
    GOOD("node/7/roll");
    GOOD("\x00");
    GOOD("\x7F");
    GOOD("\xC2\x80");         /* U+0080 */
    GOOD("\xDF\xBF");         /* U+07FF */
    GOOD("\xE0\xA0\x80");     /* U+0800 */
    GOOD("\xED\x9F\xBF");     /* U+D7FF */
    GOOD("\xEE\x80\x80");     /* U+E000 */
    GOOD("\xEF\xBF\xBF");     /* U+FFFF */
    GOOD("\xF0\x90\x80\x80"); /* U+10000 */
    GOOD("\xF4\x8F\xBF\xBF"); /* U+10FFFF */

    BAD("\x80");             /* a continuation byte first */
    BAD("\xC0\xAF");         /* overlong */
    BAD("\xC1\xBF");         /* overlong */
    BAD("\xE0\x9F\xBF");     /* overlong */
    BAD("\xF0\x8F\xBF\xBF"); /* overlong */
    BAD("\xED\xA0\x80");     /* a surrogate */
    BAD("\xF4\x90\x80\x80"); /* above U+10FFFF */
    BAD("\xF5\x80\x80\x80");
    BAD("\xFF");
    BAD("\xC2");         /* cut short */
    BAD("\xE2\x82");     /* cut short */
    BAD("\xF0\x9F\x98"); /* cut short */
    BAD("\xC2\x41");     /* the second byte is not a continuation */
    BAD("\xE2\x82\x41"); /* the third byte is not a continuation */
    BAD("ok\xF0\x9F\x98\x41");

    /* A key cut short must not take the missing byte from the body. */
    uint8_t in[RF_HEADER_SIZE + 2] = {0};
    in[0] = 2;
    in[4] = RF_REQUEST;
    in[6] = 1;
    in[RF_HEADER_SIZE] = 0xC2;
    in[RF_HEADER_SIZE + 1] = 0x80;
    rf_frame frame;
    size_t size = 0;
    CHECK(rf_frame_decode(in, sizeof in, MAX, &frame, &size) == RF_ERR_KEY);
}

int main(void) {
    test_preface();
    test_known_bytes();
    test_every_prefix_needs_more();
    test_two_frames_in_one_buffer();
    test_header_rules();
    test_clock_has_a_zero_id_and_no_body();
    test_header_alone();
    test_key_check();
    test_bad_header_is_refused_before_the_body();
    test_end_has_no_body();
    test_bad_key_is_refused_before_the_body();
    test_longest_length();
    test_longest_key();
    test_small_output_buffer();
    test_empty_key_and_body_may_be_null();
    test_utf8_keys();
    puts("frame codec: all tests passed");
    return 0;
}
