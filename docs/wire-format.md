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
| 5 | 1 | reserved | `0` |
| 6 | 2 | capabilities | one bit for each optional behaviour |

| Capability bit | Meaning |
|---|---|
| 0 | this peer drops repeated request ids |
| 1 to 15 | not assigned |

A peer that reads a wrong magic, a version it does not implement or a non-zero reserved byte
closes the connection.

## Additions to version 1

A new kind or flag gets its own capability bit.

- A peer sends it only after it read a preface that has the bit.
- A peer ignores capability bits that it does not know.
- Everything on this page is part of version 1 and needs no bit, the busy flag of END included.

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
| `0x03` | END | replier | no | number of replies sent | bit 0: no handler for the key. bit 1: busy, the request was refused |

Kinds `0x10` to `0x3F` are reserved for the operations that are not designed yet. Every other
value is unassigned.

## Rules

- Deadline: the argument of REQUEST is a duration, not a time of day. Each side counts it on its
  own monotonic clock, from the moment it sent or received the frame.
- Protocol error, and the peer closes the connection:
    - `length` above the largest accepted
    - key length above `length`, or a key that is not valid UTF-8
    - unknown kind, or non-zero unused flag
    - a key on a kind that has no key
    - END with both flags
    - END with a flag and an argument other than `0`
- REPLY or END for an id with no open request: dropped. Not an error.
- END with bit 0 from every peer: the outcome is `unroutable`.
- END from every peer, at least one with bit 1: the outcome is `busy`.

## Open questions

- A frame that tells a replier to stop work, when the requester no longer waits.
- Replies lost in a reconnect are not sent again: a repeat of an ended id gets no answer, and the
  requester sees `expired`. Not decided: whether the host may send the replies again.
