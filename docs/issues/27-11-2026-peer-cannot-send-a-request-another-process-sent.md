---
title: "Peer cannot send a request that another process sent"
status: open
area: python
filed: 2026-11-27
scope: S
verify: test
---

Source: Fable design of the Python layer. paas needs it with more than one API replica.

`Peer.request` makes a new id. A second process that takes over a request has to send the same
id and deadline, marked as sent before. `PeerState.request(..., sent_before=True)` does that.
`Peer` needs a `request_copy(id, key, body, deadline_ms, sent_before)` for it.

Done in 0.1.9: `Peer.request_copy`.
