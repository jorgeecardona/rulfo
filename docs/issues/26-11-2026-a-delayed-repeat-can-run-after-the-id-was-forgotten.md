---
title: "A delayed repeat can run after the id was forgotten"
status: done
fixed_in: a93ef7c
area: core
filed: 2026-11-26
scope: M
verify: test
---

Source: Fable review of commits `2a73d8c` to `b036d0c`.

A replier counts the time left from the moment it reads a copy of a request. A copy that waits in
a buffer can arrive after the replier forgot the id. The replier then runs the request again.

The model does not show this: it has one deadline that both sides see at the same instant.

Decide before the connection state machine: a longer time to keep an id, or a different rule. The
model needs two clocks to check the choice.

Fixed: the deadline is a time on the requester's clock, and the replier keeps the id for the keep
time after it (rules 7, 8 and 14). The model has one deadline per connection
(`test_an_id_kept_only_until_its_own_deadline_runs_twice`). The arithmetic of the keep time is
tested with the C core.
