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

static bool is_zero(const rf_id *id) {
    uint8_t any = 0;
    for (size_t i = 0; i < sizeof id->bytes; i++) {
        any |= id->bytes[i];
    }
    return any == 0;
}

/* The rules that need only the header fields. Decode and encode share them. */
static rf_status check_header(const rf_header *header) {
    uint8_t flags = header->flags;
    size_t key_len = header->key_len;
    size_t body_len = header->body_len;
    switch (header->kind) {
    case RF_REQUEST:
        if ((flags & ~RF_REQUEST_REPEAT) != 0) {
            return RF_ERR_FLAGS;
        }
        return key_len == 0 ? RF_ERR_KEY : RF_OK;
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
        if (body_len != 0) {
            return RF_ERR_BODY;
        }
        return flags != 0 && header->argument != 0 ? RF_ERR_ARGUMENT : RF_OK;
    case RF_CLOCK:
        if (flags != 0) {
            return RF_ERR_FLAGS;
        }
        if (key_len != 0) {
            return RF_ERR_KEY;
        }
        if (body_len != 0) {
            return RF_ERR_BODY;
        }
        return is_zero(&header->id) ? RF_OK : RF_ERR_ID;
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

rf_status rf_header_decode(const uint8_t *in, size_t len, uint32_t max_length,
                           rf_header *header) {
    if (len < 4) {
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
        return RF_NEED_MORE;
    }

    size_t key_len = (size_t)load_le(in + 6, 2);
    if (key_len > length) {
        return RF_ERR_KEY_LENGTH;
    }
    rf_header read = {.kind = in[4],
                      .flags = in[5],
                      .argument = load_le(in + 24, 8),
                      .key_len = key_len,
                      .body_len = length - key_len};
    memcpy(read.id.bytes, in + 8, sizeof read.id.bytes);
    rf_status status = check_header(&read);
    if (status != RF_OK) {
        return status;
    }
    *header = read;
    return RF_OK;
}

rf_status rf_header_encode(const rf_header *header, uint32_t max_length,
                           uint8_t out[RF_HEADER_SIZE]) {
    size_t key_len = header->key_len;
    size_t body_len = header->body_len;
    if (key_len > UINT16_MAX) {
        return RF_ERR_KEY_LENGTH;
    }
    /* The first two tests keep the sum of the third from wrapping around. */
    if (key_len > max_length || body_len > max_length - key_len ||
        key_len + body_len > SIZE_MAX - RF_HEADER_SIZE) {
        return RF_ERR_LENGTH;
    }
    rf_status status = check_header(header);
    if (status != RF_OK) {
        return status;
    }
    store_le(out, key_len + body_len, 4);
    out[4] = header->kind;
    out[5] = header->flags;
    store_le(out + 6, key_len, 2);
    memcpy(out + 8, header->id.bytes, sizeof header->id.bytes);
    store_le(out + 24, header->argument, 8);
    return RF_OK;
}

rf_status rf_key_check(rf_bytes key) {
    return is_utf8(key.ptr, key.len) ? RF_OK : RF_ERR_KEY;
}

rf_status rf_frame_decode(const uint8_t *in, size_t len, uint32_t max_length, rf_frame *frame,
                          size_t *size) {
    rf_header header;
    rf_status status = rf_header_decode(in, len, max_length, &header);
    if (status == RF_NEED_MORE) {
        *size = RF_HEADER_SIZE;
    }
    if (status != RF_OK) {
        return status;
    }
    const uint8_t *key = in + RF_HEADER_SIZE;
    /* Before the wait for the body: a bad key must not make the caller buffer a long frame. */
    if (len - RF_HEADER_SIZE >= header.key_len && !is_utf8(key, header.key_len)) {
        return RF_ERR_KEY;
    }

    size_t total = (size_t)RF_HEADER_SIZE + header.key_len + header.body_len;
    if (len < total) {
        *size = total;
        return RF_NEED_MORE;
    }
    frame->kind = header.kind;
    frame->flags = header.flags;
    frame->id = header.id;
    frame->argument = header.argument;
    frame->key = (rf_bytes){.ptr = key, .len = header.key_len};
    frame->body = (rf_bytes){.ptr = key + header.key_len, .len = header.body_len};
    *size = total;
    return RF_OK;
}

rf_status rf_frame_encode(const rf_frame *frame, uint32_t max_length, uint8_t *out, size_t cap,
                          size_t *size) {
    rf_header header = {.kind = frame->kind,
                        .flags = frame->flags,
                        .id = frame->id,
                        .argument = frame->argument,
                        .key_len = frame->key.len,
                        .body_len = frame->body.len};
    uint8_t bytes[RF_HEADER_SIZE];
    rf_status status = rf_header_encode(&header, max_length, bytes);
    if (status != RF_OK) {
        return status;
    }
    if (rf_key_check(frame->key) != RF_OK) {
        return RF_ERR_KEY;
    }

    size_t total = (size_t)RF_HEADER_SIZE + header.key_len + header.body_len;
    if (cap < total) {
        *size = total;
        return RF_ERR_SPACE;
    }
    memcpy(out, bytes, sizeof bytes);
    /* memcpy from NULL is undefined, even for 0 bytes. */
    if (header.key_len > 0) {
        memcpy(out + RF_HEADER_SIZE, frame->key.ptr, header.key_len);
    }
    if (header.body_len > 0) {
        memcpy(out + RF_HEADER_SIZE + header.key_len, frame->body.ptr, header.body_len);
    }
    *size = total;
    return RF_OK;
}
