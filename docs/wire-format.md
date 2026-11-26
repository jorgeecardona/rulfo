# Wire format

This page defines the bytes two rulfo peers exchange. Version 1. It covers the `request`
operation only (see [Operations](operations.md)).

## Transport

rulfo needs an ordered, reliable byte stream: a unix socket, TCP or a WebSocket. Bytes are lost
only when the connection ends. rulfo does not encrypt.

## Byte order

Every integer is unsigned and little-endian. This is fixed for all versions.

## Preface

Each side sends 8 bytes once, before any frame.

| Offset | Size | Field | Value |
|---|---|---|---|
| 0 | 4 | magic | `52 55 4C 46` (`RULF`) |
| 4 | 1 | version | `1` |
| 5 | 1 | capabilities | bit 0: this peer drops repeated request ids |
| 6 | 2 | reserved | `0` |

A peer that reads a wrong magic or a version it does not implement closes the connection. There
is no negotiation in version 1.

## Frame

A frame is a 32-byte header, then the key, then the body.

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | 4 | length | bytes after the header: key plus body |
| 4 | 1 | kind | see below |
| 5 | 1 | flags | per kind, unused bits are `0` |
| 6 | 2 | key length | bytes of the key, at most `length` |
| 8 | 16 | id | the request id |
| 24 | 8 | argument | per kind |

The key is UTF-8. The body is opaque bytes. The largest `length` a peer accepts is a setting,
16 MiB by default. A longer frame is a protocol error.

## Kinds

| Kind | Name | Sender | Key | Argument | Flags |
|---|---|---|---|---|---|
| `0x01` | REQUEST | requester | yes | milliseconds until the deadline | bit 0: sent again after a reconnect |
| `0x02` | REPLY | replier | no | number of this reply, from 0 | bit 0: the body is an error |
| `0x03` | END | replier | no | number of replies sent | bit 0: no handler for the key |

Kinds `0x10` to `0x3F` are reserved for the operations that are not designed yet. Every other
value is unassigned.

## Rules

- Deadline: the argument of REQUEST is a duration, not a time of day. Each side counts it on its
  own monotonic clock, from the moment it sent or received the frame.
- Unknown kind, non-zero unused flag, or key length above `length`: protocol error. The peer
  closes the connection.
- REPLY or END for an id with no open request: dropped. Not an error.
- END with bit 0 from every peer: the outcome is `unroutable`.

## Open questions

- A replier whose store of ids is full of requests that have not expired. It cannot forget one
  (rule 7 of `request`). The frame that refuses a new request is not defined.
- A frame that tells a replier to stop work, when the requester no longer waits.
- Replies lost in a reconnect are not sent again. The requester sees `expired`.
