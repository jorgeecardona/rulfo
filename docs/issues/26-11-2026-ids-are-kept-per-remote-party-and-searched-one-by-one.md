---
title: "Ids are kept for one remote party and searched one by one"
status: open
area: core
filed: 2026-11-26
scope: M
verify: test
---

Source: Fable review of commits `d3cd3c4` to `6adcdee`.

The id store is part of `rf_peer`. A copy that reaches another `rf_peer` runs again. A replier
that takes one request id from several remote parties needs a store that its peers share.

Every call that looks up an id or a time reads all `max_ids` slots. A store of many thousand ids
needs a hash table and an ordered list of times.

Do both when a user of rulfo needs one of them.
