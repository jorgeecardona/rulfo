---
title: "Server.aclose closes one party at a time"
status: open
area: python
filed: 2026-11-27
scope: S
verify: test
---

Source: Fable review of `Server`.

A handler that takes long to finish its cancellation keeps the next parties connected, and their
handlers running, for that time. A handler that never finishes blocks the others for good.

Fix: run the part of `Peer.aclose` before its first await on every party, then wait for each.
