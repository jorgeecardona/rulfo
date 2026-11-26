/* rulfo: the frame codec of wire format version 1 (docs/wire-format.md).
 *
 * The functions do no I/O and allocate no memory. They read and write the caller's buffers.
 */
#ifndef RULFO_H
#define RULFO_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define RF_VERSION 1
#define RF_PREFACE_SIZE 8
#define RF_HEADER_SIZE 32

/* The default for the largest `length` (key plus body) a peer accepts. */
#define RF_DEFAULT_MAX_LENGTH (16u * 1024u * 1024u)

/* Capability bit of the preface: this peer drops repeated request ids. */
#define RF_CAP_DROPS_REPEATS 0x0001u

#define RF_REQUEST_REPEAT 0x01u
#define RF_REPLY_ERROR 0x01u
#define RF_END_NO_HANDLER 0x01u
#define RF_END_BUSY 0x02u

typedef enum {
    RF_OK = 0,
    RF_NEED_MORE,      /* not an error: call again with more bytes */
    RF_ERR_SPACE,      /* the output buffer is too small */
    RF_ERR_MAGIC,      /* the preface does not start with RULF */
    RF_ERR_VERSION,    /* a version this library does not implement */
    RF_ERR_RESERVED,   /* a non-zero reserved byte */
    RF_ERR_LENGTH,     /* key plus body is above the largest accepted */
    RF_ERR_KEY_LENGTH, /* the key length is above `length` or above 65535 */
    RF_ERR_KIND,       /* unknown kind */
    RF_ERR_FLAGS,      /* unknown flag, or flags that exclude each other */
    RF_ERR_KEY,        /* a key that is not UTF-8, or is on a kind without one, or is missing */
    RF_ERR_BODY,       /* a body on END or CLOCK */
    RF_ERR_ARGUMENT,   /* END with a flag and a reply count other than 0 */
    RF_ERR_ID          /* CLOCK with an id that is not zero */
} rf_status;

typedef enum {
    RF_REQUEST = 0x01,
    RF_REPLY = 0x02,
    RF_END = 0x03,
    RF_CLOCK = 0x04
} rf_kind;

/* Bytes the caller owns. `ptr` may be NULL when `len` is 0. */
typedef struct {
    const uint8_t *ptr;
    size_t len;
} rf_bytes;

typedef struct {
    uint8_t bytes[16];
} rf_id;

typedef struct {
    uint8_t kind; /* an rf_kind */
    uint8_t flags;
    rf_id id;
    /* REQUEST: the deadline, in milliseconds on the requester's clock. REPLY: its number.
     * END: replies sent. CLOCK: the sender's clock in milliseconds. */
    uint64_t argument;
    rf_bytes key;
    rf_bytes body;
} rf_frame;

/* The 32 bytes before the key and the body. */
typedef struct {
    uint8_t kind; /* an rf_kind */
    uint8_t flags;
    rf_id id;
    uint64_t argument; /* as in rf_frame */
    size_t key_len;
    size_t body_len;
} rf_header;

/* Writes the 8 bytes each side sends before its first frame. */
void rf_preface_encode(uint16_t capabilities, uint8_t out[RF_PREFACE_SIZE]);

/* Reads the preface of the other side from the first RF_PREFACE_SIZE bytes of `in`.
 * Returns RF_NEED_MORE when `len` is smaller. `capabilities` is set only on RF_OK and keeps
 * the bits this library does not know. */
rf_status rf_preface_decode(const uint8_t *in, size_t len, uint16_t *capabilities);

/* Reads a header from the start of `in`, for a caller that streams or skips the key and body.
 *
 * RF_OK: `header` is set. The key and the body are the next `key_len` and `body_len` bytes. The
 * key is not checked: pass it to rf_key_check.
 * RF_NEED_MORE: `len` is below RF_HEADER_SIZE. `header` is not changed.
 * Any other status is a protocol error. `header` is not changed. A `length` above `max_length`
 * is refused after 4 bytes. */
rf_status rf_header_decode(const uint8_t *in, size_t len, uint32_t max_length, rf_header *header);

/* Writes the RF_HEADER_SIZE bytes of `header`. The caller sends the key and the body after them.
 * The key is not checked. On any status other than RF_OK nothing is written. */
rf_status rf_header_encode(const rf_header *header, uint32_t max_length,
                           uint8_t out[RF_HEADER_SIZE]);

/* RF_OK when `key` is well-formed UTF-8, an empty key included. RF_ERR_KEY otherwise. */
rf_status rf_key_check(rf_bytes key);

/* Reads one frame from the start of `in`. `max_length` is the largest key plus body accepted.
 *
 * RF_OK: `frame` is set, its key and body point into `in`, and `size` is the bytes it used. The
 * key and the body are valid while the caller does not change `in`.
 * RF_NEED_MORE: `size` is the number of bytes needed, as far as the input tells: the header
 * size, then the size of the whole frame. `frame` is not changed.
 * Any other status is a protocol error: close the connection. `frame` and `size` are not changed.
 * A `length` above `max_length` is refused after 4 bytes, a bad header after RF_HEADER_SIZE
 * bytes, and a bad key as soon as the whole key arrived: none of them waits for the body. */
rf_status rf_frame_decode(const uint8_t *in, size_t len, uint32_t max_length, rf_frame *frame,
                          size_t *size);

/* Writes one frame to `out`, which must not overlap the key or the body.
 *
 * RF_OK: `size` is the bytes written. RF_ERR_SPACE: `size` is the bytes needed.
 * A frame that rf_frame_decode would refuse is refused with the same status.
 * On any status other than RF_OK nothing is written to `out`. */
rf_status rf_frame_encode(const rf_frame *frame, uint32_t max_length, uint8_t *out, size_t cap,
                          size_t *size);

#ifdef __cplusplus
}
#endif

#endif /* RULFO_H */
