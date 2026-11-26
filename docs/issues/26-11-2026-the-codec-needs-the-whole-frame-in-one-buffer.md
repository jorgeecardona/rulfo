---
title: "The codec needs the whole frame in one buffer"
status: done
fixed_in: 4d93287
area: core
filed: 2026-11-26
scope: S
verify: test
---

Source: Fable review of commits `2a73d8c` to `b036d0c`.

`rf_frame_decode` reads a frame only when it is complete, and `rf_frame_encode` copies the body.
A connection would need `32 + max_length` bytes (16 MiB by default) for each direction, also to
drop a REPLY for an unknown id.

Add, with the connection state machine that uses them:

- `rf_header_decode` and `rf_header_encode`, so the body can be streamed, skipped or sent with
  `writev`.
- A public key check.
