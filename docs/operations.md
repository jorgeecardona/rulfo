# Operations

This page lists what a program can ask rulfo to do. Only `request` is designed in detail.
The bytes are in [Wire format](wire-format.md).

## request

A requester sends one request to a key. Every replier that handles the key sends zero, one or
many replies, then an end. The requester always gets exactly one outcome.

| Outcome | When |
|---|---|
| `replied` | Every replier sent its end, and every reply it sent arrived. The number of replies can be zero. |
| `expired` | The deadline passed first. Replies that arrived before it were delivered. |
| `unroutable` | No replier handles the key. The requester learns this at once, not at the deadline. |

Rules:

1. A request has a deadline. There is no request without one.
2. A request has an id, chosen by the requester. 16 bytes. A replier runs one id at most once.
3. A reply carries the id of its request. A reply for an unknown or ended id is dropped.
4. A replier is not required to reply. It is required to end. A replier that never ends is
   covered by the deadline.
5. An end carries the number of replies the replier sent. The requester accepts the end only when
   it received that many. Otherwise the outcome is `expired` at the deadline.
6. After a reconnect, the requester sends an open request again, with the same id and the time
   that is left. It does this only for a replier that drops repeated ids.
7. A replier keeps an id until the deadline of its request has passed. It never forgets an id
   earlier, and never while the handler runs.
8. A replier drops a request whose deadline has passed.

What a replier that crashed looks like to the requester: no end arrives, and the outcome is
`expired`.

## Not designed in detail

These three operations are planned. Their frames, rules and models do not exist yet.

- Latest value: publish a value under a key. A late subscriber gets the latest value. A delete
  removes it. Each subscriber chooses what happens when its queue is full: drop or block.
- Work: offer a task to one worker. The worker acknowledges it. A task with no acknowledgement
  is offered again, so a task can run twice and must be idempotent.
- Presence: learn when a peer appears and when it is gone.

## Out of scope

- Discovery. The address of the peer is given.
- Encryption. The transport does it (socket permissions, TLS).
- Storage. The host program saves and loads. The core only says what to save.
