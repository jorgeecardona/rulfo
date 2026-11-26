#include "rulfo.h"

#include <stdbool.h>
#include <string.h>

static const uint8_t magic[4] = {'R', 'U', 'L', 'F'};

/* Every integer on the wire is little-endian, on every host. */
static uint64_t load_le(const uint8_t *in, size_t width) {
    uint64_t value = 0;
    for (size_t i = width; i > 0; i--) {
        value = (value << 8) | in[i - 1];
    }
    return value;
}

static void store_le(uint8_t *out, uint64_t value, size_t width) {
    for (size_t i = 0; i < width; i++) {
        out[i] = (uint8_t)(value >> (8 * i));
    }
}

static bool between(uint8_t byte, uint8_t low, uint8_t high) {
    return byte >= low && byte <= high;
}

/* The well-formed byte sequences of RFC 3629: no overlong form, no surrogate, no code point
 * above U+10FFFF. */
static bool is_utf8(const uint8_t *s, size_t len) {
    size_t i = 0;
    while (i < len) {
        uint8_t first = s[i];
        size_t rest;
        uint8_t low = 0x80;
        uint8_t high = 0xBF;

        if (first <= 0x7F) {
            rest = 0;
        } else if (between(first, 0xC2, 0xDF)) {
            rest = 1;
        } else if (between(first, 0xE0, 0xEF)) {
            rest = 2;
            if (first == 0xE0) {
                low = 0xA0;
            } else if (first == 0xED) {
                high = 0x9F;
            }
        } else if (between(first, 0xF0, 0xF4)) {
            rest = 3;
            if (first == 0xF0) {
                low = 0x90;
            } else if (first == 0xF4) {
                high = 0x8F;
            }
        } else {
            return false;
        }

        if (rest > len - i - 1) {
            return false;
        }
        for (size_t k = 1; k <= rest; k++) {
            /* Only the second byte has a narrower range. */
            if (!between(s[i + k], k == 1 ? low : 0x80, k == 1 ? high : 0xBF)) {
                return false;
            }
        }
        i += rest + 1;
    }
    return true;
}

/* The rules that need only the header fields. Decode and encode share them. */
static rf_status check_header(uint8_t kind, uint8_t flags, size_t key_len, uint64_t argument) {
    switch (kind) {
    case RF_REQUEST:
        return (flags & ~RF_REQUEST_REPEAT) != 0 ? RF_ERR_FLAGS : RF_OK;
    case RF_REPLY:
        if ((flags & ~RF_REPLY_ERROR) != 0) {
            return RF_ERR_FLAGS;
        }
        return key_len != 0 ? RF_ERR_KEY : RF_OK;
    case RF_END:
        if (flags != 0 && flags != RF_END_NO_HANDLER && flags != RF_END_BUSY) {
            return RF_ERR_FLAGS;
        }
        if (key_len != 0) {
            return RF_ERR_KEY;
        }
        return flags != 0 && argument != 0 ? RF_ERR_ARGUMENT : RF_OK;
    default:
        return RF_ERR_KIND;
    }
}

void rf_preface_encode(uint16_t capabilities, uint8_t out[RF_PREFACE_SIZE]) {
    memcpy(out, magic, sizeof magic);
    out[4] = RF_VERSION;
    out[5] = 0;
    store_le(out + 6, capabilities, 2);
}

rf_status rf_preface_decode(const uint8_t *in, size_t len, uint16_t *capabilities) {
    if (len < RF_PREFACE_SIZE) {
        return RF_NEED_MORE;
    }
    if (memcmp(in, magic, sizeof magic) != 0) {
        return RF_ERR_MAGIC;
    }
    if (in[4] != RF_VERSION) {
        return RF_ERR_VERSION;
    }
    if (in[5] != 0) {
        return RF_ERR_RESERVED;
    }
    *capabilities = (uint16_t)load_le(in + 6, 2);
    return RF_OK;
}

rf_status rf_frame_decode(const uint8_t *in, size_t len, uint32_t max_length, rf_frame *frame,
                          size_t *size) {
    if (len < 4) {
        *size = RF_HEADER_SIZE;
        return RF_NEED_MORE;
    }
    uint32_t length = (uint32_t)load_le(in, 4);
    if (length > max_length) {
        return RF_ERR_LENGTH;
    }
#if SIZE_MAX <= UINT32_MAX
    if (length > SIZE_MAX - RF_HEADER_SIZE) {
        return RF_ERR_LENGTH;
    }
#endif
    if (len < RF_HEADER_SIZE) {
        *size = RF_HEADER_SIZE;
        return RF_NEED_MORE;
    }

    uint8_t kind = in[4];
    uint8_t flags = in[5];
    size_t key_len = (size_t)load_le(in + 6, 2);
    uint64_t argument = load_le(in + 24, 8);
    if (key_len > length) {
        return RF_ERR_KEY_LENGTH;
    }
    rf_status status = check_header(kind, flags, key_len, argument);
    if (status != RF_OK) {
        return status;
    }

    size_t total = (size_t)RF_HEADER_SIZE + length;
    if (len < total) {
        *size = total;
        return RF_NEED_MORE;
    }
    const uint8_t *key = in + RF_HEADER_SIZE;
    if (!is_utf8(key, key_len)) {
        return RF_ERR_KEY;
    }

    frame->kind = kind;
    frame->flags = flags;
    memcpy(frame->id.bytes, in + 8, sizeof frame->id.bytes);
    frame->argument = argument;
    frame->key = (rf_bytes){.ptr = key, .len = key_len};
    frame->body = (rf_bytes){.ptr = key + key_len, .len = length - key_len};
    *size = total;
    return RF_OK;
}

rf_status rf_frame_encode(const rf_frame *frame, uint32_t max_length, uint8_t *out, size_t cap,
                          size_t *size) {
    size_t key_len = frame->key.len;
    size_t body_len = frame->body.len;
    if (key_len > UINT16_MAX) {
        return RF_ERR_KEY_LENGTH;
    }
    /* The first two tests keep the sum of the third from wrapping around. */
    if (key_len > max_length || body_len > max_length - key_len ||
        key_len + body_len > SIZE_MAX - RF_HEADER_SIZE) {
        return RF_ERR_LENGTH;
    }
    rf_status status = check_header(frame->kind, frame->flags, key_len, frame->argument);
    if (status != RF_OK) {
        return status;
    }
    if (!is_utf8(frame->key.ptr, key_len)) {
        return RF_ERR_KEY;
    }

    size_t total = (size_t)RF_HEADER_SIZE + key_len + body_len;
    if (cap < total) {
        *size = total;
        return RF_ERR_SPACE;
    }
    store_le(out, key_len + body_len, 4);
    out[4] = frame->kind;
    out[5] = frame->flags;
    store_le(out + 6, key_len, 2);
    memcpy(out + 8, frame->id.bytes, sizeof frame->id.bytes);
    store_le(out + 24, frame->argument, 8);
    /* memcpy from NULL is undefined, even for 0 bytes. */
    if (key_len > 0) {
        memcpy(out + RF_HEADER_SIZE, frame->key.ptr, key_len);
    }
    if (body_len > 0) {
        memcpy(out + RF_HEADER_SIZE + key_len, frame->body.ptr, body_len);
    }
    *size = total;
    return RF_OK;
}
