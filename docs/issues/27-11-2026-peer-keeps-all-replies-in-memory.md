---
title: "Peer keeps all replies of a request in memory"
status: open
area: python
filed: 2026-11-27
scope: S
verify: test
---

Source: Fable design of the Python layer.

`Peer.request` returns when it has the outcome, with every reply. A request with many replies, or
one that runs for long, needs `Peer.stream()`: an async iterator of replies with a bounded queue.

The wire has no flow control per request. A full queue stops the reader, and with it every other
request of that `Peer`, until the caller reads or the deadline passes.
