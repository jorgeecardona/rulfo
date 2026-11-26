/* libFuzzer target: any input is refused, asks for more, or decodes to a frame that encodes
 * back to the same bytes. */
#include "rulfo.h"

#include <stdlib.h>
#include <string.h>

#define REQUIRE(condition) \
    do {                   \
        if (!(condition)) { \
            abort();       \
        }                  \
    } while (0)

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static void preface(const uint8_t *data, size_t size) {
    uint16_t capabilities = 0;
    if (rf_preface_decode(data, size, &capabilities) == RF_OK) {
        uint8_t out[RF_PREFACE_SIZE];
        rf_preface_encode(capabilities, out);
        REQUIRE(memcmp(out, data, sizeof out) == 0);
    }
}

/* The header functions refuse what the frame decoder refuses, apart from the key bytes. */
static void header_agrees(const uint8_t *data, size_t size, uint32_t max_length,
                          rf_status frame_status) {
    rf_header header;
    rf_status status = rf_header_decode(data, size, max_length, &header);
    if (status != RF_OK) {
        REQUIRE(frame_status == status);
        return;
    }
    REQUIRE(frame_status == RF_OK || frame_status == RF_NEED_MORE || frame_status == RF_ERR_KEY);
    uint8_t out[RF_HEADER_SIZE];
    REQUIRE(rf_header_encode(&header, max_length, out) == RF_OK);
    REQUIRE(memcmp(out, data, sizeof out) == 0);
}

static void frames(const uint8_t *data, size_t size, uint32_t max_length) {
    size_t at = 0;
    for (;;) {
        rf_frame frame;
        size_t used = 0;
        rf_status status = rf_frame_decode(data + at, size - at, max_length, &frame, &used);
        header_agrees(data + at, size - at, max_length, status);
        if (status == RF_NEED_MORE) {
            REQUIRE(used > size - at);
            REQUIRE(used <= (size_t)RF_HEADER_SIZE + max_length);
            return;
        }
        if (status != RF_OK) {
            return;
        }
        REQUIRE(used >= RF_HEADER_SIZE && used <= size - at);
        REQUIRE(frame.key.ptr == data + at + RF_HEADER_SIZE);
        REQUIRE(frame.body.ptr == frame.key.ptr + frame.key.len);
        REQUIRE(RF_HEADER_SIZE + frame.key.len + frame.body.len == used);

        uint8_t *out = malloc(used);
        REQUIRE(out != NULL);
        size_t written = 0;
        REQUIRE(rf_frame_encode(&frame, max_length, out, used, &written) == RF_OK);
        REQUIRE(written == used);
        REQUIRE(memcmp(out, data + at, used) == 0);
        free(out);
        at += used;
    }
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    preface(data, size);
    frames(data, size, 64); /* a small limit, so the limit is reached often */
    frames(data, size, RF_DEFAULT_MAX_LENGTH);
    return 0;
}
