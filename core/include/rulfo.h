/* rulfo: wire format version 1 (docs/wire-format.md) and the rules of docs/operations.md.
 *
 * Two parts: the frame codec, and rf_peer, which holds the state for one remote party.
 * The functions do no I/O, read no clock and allocate no memory. The caller moves the bytes,
 * passes the time and gives the memory.
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
    RF_ERR_ID,         /* CLOCK with an id that is not zero */
    RF_WOULD_BLOCK,    /* not an error: take the bytes of rf_output, then call again */
    RF_ERR_HANDSHAKE,  /* the CLOCK is late, repeated, or another frame came before it */
    RF_ERR_STATE,      /* the call is not valid now */
    RF_ERR_CONFIG,     /* a setting that is out of range */
    RF_ERR_MEMORY,     /* the memory is too small or not aligned */
    RF_ERR_NOT_READY,  /* no connection, or its handshake is not done */
    RF_ERR_FULL,       /* too many open requests */
    RF_ERR_EXPIRED,    /* the deadline has passed */
    RF_ERR_UNKNOWN_ID, /* no request with this id was taken, or it is forgotten */
    RF_OFFLINE,        /* not an error: recorded; keep the body, send nothing */
    RF_DROPPED         /* not an error: the refusal is not sent */
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

/* ---- One remote party ------------------------------------------------------------------
 *
 * An rf_peer holds what outlives a connection, and at most one connection at a time. Every
 * `now` is milliseconds on one clock that never goes back, does not jump and counts the time
 * the machine is suspended (rule 14 in docs/operations.md).
 *
 * Use, for each connection:
 *   1. rf_connected.
 *   2. Send what rf_output gives, now and after every other call. Send it before the bytes of
 *      a header that a later call gives.
 *   3. Pass received bytes to rf_input until it returns RF_NEED_MORE.
 *   4. Call rf_poll until it gives RF_EV_NONE, again at rf_next_time. Also without a
 *      connection: requests expire there.
 *   5. On RF_EV_CLOSE, or when the transport ends: rf_disconnected. Read nothing more from it.
 *
 * The core keeps no key and no body. A call that fails changes nothing.
 *
 * Frames must not interleave on the wire. Hold one write lock from the call that writes a header
 * until the last byte of its key and body is written, and send what rf_output gives only under
 * that lock: rf_input can queue a frame while another is half written.
 *
 * Read rf_next_time again after every call: a call can move it earlier.
 *
 * One rf_peer stands for one remote party and holds the ids that party sent. Every copy of an
 * id must reach the same rf_peer, over all connections and all processes of that party: a copy
 * that reaches another rf_peer runs again. An accepting side therefore finds the rf_peer of a
 * new connection by its own means, for example the authenticated name of the caller.
 */

typedef struct rf_peer rf_peer;

typedef struct {
    uint32_t max_length;      /* largest key plus body accepted */
    uint32_t max_out;         /* bytes of core-made frames that can wait, at least 40 */
    uint32_t handshake_ms;    /* longest wait for the other CLOCK */
    uint32_t keep_ms;         /* how long an id is kept after its deadline, >= handshake_ms */
    uint32_t max_requests;    /* open requests this side can have */
    /* Request ids this side can keep as the replier. An id stays until its deadline plus
     * keep_ms, so new requests above max_ids / (deadline + keep_ms) per second are refused as
     * busy: 4096 ids, 10 s deadlines and 60 s keep allow 58 per second. Calls take time in
     * proportion to max_ids. */
    uint32_t max_ids;
    uint32_t max_deadline_ms; /* a request with a deadline further away is refused */
} rf_config;

typedef enum {
    RF_EV_NONE = 0,
    RF_EV_READY,   /* the other CLOCK arrived: the connection can be used */
    RF_EV_CLOSE,   /* close the transport and call rf_disconnected; `status` says why */
    RF_EV_REPLY,   /* a reply to a request: `id`, `number`, `flags`, `body` */
    RF_EV_OUTCOME, /* a request ended: `id`, `outcome`, `number` replies arrived */
    RF_EV_RESEND,  /* send this open request again: rf_request with `id`, `deadline`, repeat 1 */
    /* Run this: `id`, `key`, `body`, `deadline`, `flags`. Every one needs rf_end or rf_refuse,
     * also after the deadline or a failed handler: until then the id holds a slot. */
    RF_EV_REQUEST,
    /* `id` came again, marked, on a new connection: see rf_reply_again. `number` replies and
     * `ended` say what was sent when the copy was read. Given once for each connection. */
    RF_EV_REPEAT,
    RF_EV_FORGOT   /* `id` is forgotten: the stored replies can be freed */
} rf_event_kind;

typedef enum {
    RF_COMPLETED = 0,
    RF_BUSY,      /* refused: it did not run */
    RF_EXPIRED,   /* the deadline passed first: it may have run */
    RF_UNROUTABLE /* no handler for the key: it did not run */
} rf_outcome;

/* What happened. Only the fields named at the kind are set. `key` and `body` point into the
 * `in` of rf_input and are valid until the caller changes those bytes. */
typedef struct {
    uint8_t kind;    /* an rf_event_kind */
    uint8_t flags;   /* REPLY: RF_REPLY_ERROR or 0. REQUEST: RF_REQUEST_REPEAT or 0 */
    uint8_t outcome; /* an rf_outcome */
    uint8_t ended;   /* REPEAT: not 0 when the end was sent */
    rf_status status;
    rf_id id;
    uint64_t number;
    uint64_t deadline; /* on the caller's clock */
    rf_bytes key;
    rf_bytes body;
} rf_event;

/* The default settings: 16 MiB, 1024 bytes, 5 s, 60 s, 64 requests, 4096 ids and 1 hour. */
void rf_config_default(rf_config *config);

/* The bytes of memory a peer with `config` needs. 0 when a setting is out of range. */
size_t rf_peer_size(const rf_config *config);

/* Makes a peer in `memory`, which must be aligned as malloc aligns and stay valid while the
 * peer is used. RF_ERR_CONFIG or RF_ERR_MEMORY: `peer` is not set. */
rf_status rf_peer_init(void *memory, size_t len, const rf_config *config, rf_peer **peer);

/* Starts a connection. A connection that was open is forgotten. */
void rf_connected(rf_peer *peer, uint64_t now);

/* Ends the connection. Bytes not yet taken from rf_output are dropped. */
void rf_disconnected(rf_peer *peer);

/* Reads at most one frame (or the preface) from `in`.
 *
 * RF_OK: `used` bytes are consumed and `event` says what happened, often RF_EV_NONE. Pass the
 * remaining bytes again.
 * RF_NEED_MORE: nothing is consumed. Add bytes after these and call again.
 * RF_WOULD_BLOCK: nothing is consumed. Take the bytes of rf_output and call again.
 * A request that this side cannot accept is refused as busy by the core, without an event.
 * RF_ERR_STATE: there is no open connection.
 * Any other status is a protocol error: `event` is RF_EV_CLOSE and the connection is over. */
rf_status rf_input(rf_peer *peer, const uint8_t *in, size_t len, uint64_t now, size_t *used,
                   rf_event *event);

/* Starts a request, or sends an open one again. Writes its header: the caller sends the header,
 * then `key.len` bytes of the key and `body_len` bytes of the body. The outcome comes later as
 * RF_EV_OUTCOME, once.
 *
 * `deadline` is on the caller's clock. `repeat` is not 0 when an earlier copy of `id` was sent
 * and has no end: for an RF_EV_RESEND, and for a copy that another process sent. `id` is 16
 * bytes that no other request uses.
 *
 * RF_ERR_NOT_READY: no connection that can be used. With no earlier copy, the caller can report
 * the request as unroutable.
 * RF_WOULD_BLOCK: take the bytes of rf_output first.
 * RF_ERR_EXPIRED, RF_ERR_FULL, and a codec status for a bad key or length.
 * RF_ERR_STATE: `id` already has a copy without an end on this connection; or it is open and
 * `repeat` is 0 or the deadline differs; or `repeat` is set and the other side does not drop
 * repeated ids. */
rf_status rf_request(rf_peer *peer, rf_id id, rf_bytes key, size_t body_len, uint64_t deadline,
                     int repeat, uint64_t now, uint8_t header[RF_HEADER_SIZE]);

/* The replier side. After RF_EV_REQUEST, the caller sends any number of replies and then one
 * end, or refuses. Each call writes a header; the caller sends it, then `body_len` bytes.
 *
 * rf_reply and rf_end return RF_OFFLINE without a connection that can be used: the core counted
 * the frame, the caller keeps the body for rf_reply_again and sends nothing now.
 * RF_ERR_UNKNOWN_ID, RF_ERR_STATE after the end, RF_WOULD_BLOCK (take the bytes of rf_output
 * first; nothing is counted), and a codec status for a bad flag or length. */
rf_status rf_reply(rf_peer *peer, rf_id id, size_t body_len, uint8_t flags,
                   uint8_t header[RF_HEADER_SIZE]);
rf_status rf_end(rf_peer *peer, rf_id id, uint8_t header[RF_HEADER_SIZE]);

/* Refuses a request instead of running it, with RF_END_BUSY or RF_END_NO_HANDLER, before any
 * reply. The core forgets the id.
 * RF_OK: send the header.
 * RF_DROPPED: send nothing. The copy was marked as sent again, or the connection that brought
 * it has ended (rule 11).
 * RF_ERR_UNKNOWN_ID, RF_ERR_STATE after a reply or the end, RF_ERR_FLAGS, RF_WOULD_BLOCK. */
rf_status rf_refuse(rf_peer *peer, rf_id id, uint8_t end_flag, uint8_t header[RF_HEADER_SIZE]);

/* After RF_EV_REPEAT, to send stored replies again, or send nothing. Keep each body when
 * rf_reply is called. To replay: take the write lock; call rf_reply_again for number 0, 1, 2 and
 * so on until it returns RF_ERR_STATE; then call rf_end_again, which returns RF_ERR_STATE when
 * there is no end yet. Do not stop at the `number` of the event: a reply or the end can be sent
 * between the event and the replay, and the requester drops it then.
 * RF_ERR_NOT_READY, RF_ERR_UNKNOWN_ID, RF_ERR_STATE for a number or an end that was never sent,
 * RF_WOULD_BLOCK, and a codec status. */
rf_status rf_reply_again(rf_peer *peer, rf_id id, uint64_t number, size_t body_len,
                         uint8_t flags, uint8_t header[RF_HEADER_SIZE]);
rf_status rf_end_again(rf_peer *peer, rf_id id, uint8_t header[RF_HEADER_SIZE]);

/* Moves up to `cap` bytes that the core wants sent into `out`. Returns how many. */
size_t rf_output(rf_peer *peer, uint8_t *out, size_t cap);

/* Gives one event that is due at `now`, or RF_EV_NONE. */
void rf_poll(rf_peer *peer, uint64_t now, rf_event *event);

/* When to call rf_poll next: a time that has passed means now. UINT64_MAX: not until another
 * call changes something. */
uint64_t rf_next_time(const rf_peer *peer);

#ifdef __cplusplus
}
#endif

#endif /* RULFO_H */
