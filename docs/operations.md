# Operations

This page lists what a program can ask rulfo to do. Only `request` is designed in detail.
The bytes are in [Wire format](wire-format.md).

## request

A requester sends one request to a key. Every replier that handles the key sends zero, one or
many replies, then an end. The requester always gets exactly one outcome.

| Outcome | When |
|---|---|
| `completed` | Every replier sent its end, no replier refused, and every reply that was sent arrived. The number of replies can be zero. |
| `busy` | Every replier sent its end, and at least one refused the request. A replier that refused did not run it. |
| `expired` | The deadline passed first. The request may have run. Replies that arrived before the deadline were delivered. |
| `unroutable` | No replier handles the key. Nothing ran. The requester learns this at once, unless the connection drops first: then the outcome can be `expired`. |

With one replier, `busy` means that nothing ran. With more than one, another replier may have run
the request.

Rules:

1. A request has a deadline: a time on the requester's clock. There is no request without one.
2. A request has an id, chosen by the requester. 16 bytes. A replier runs one id at most once.
3. A reply carries the id of its request and its number, from 0. The requester drops a reply for
   an unknown or ended id. From one replier it takes only the next number: it drops a number it
   already has, and a higher one.
4. A replier is not required to reply. It is required to end. A replier that never ends is
   covered by the deadline.
5. An end carries the number of replies the replier sent. The requester accepts the end only when
   it received that many. Otherwise the outcome is `expired` at the deadline.
6. After a reconnect, the requester sends an open request again, with the same id and the same
   deadline, marked as sent again. It does this only for a replier that drops repeated ids.
7. A replier keeps the id of a request it runs until the deadline plus the keep time (a setting,
   60 s by default). It never forgets the id earlier, never while the handler runs, and never to
   make room for another id.
8. A replier drops a copy that it reads after the deadline.
9. A replier looks up the id first, and never runs a copy with a known id. If such a copy is
   marked as sent again, the replier may [send its replies again](#replies-lost-in-a-reconnect).
   Otherwise it answers nothing.
10. A replier refuses a new request that it cannot accept: its store of ids is full, or the
    deadline is longer than the replier accepts (a setting). It sends an end marked busy and does
    not run the handler. A replier that does not drop repeated ids has no store, so only the
    deadline applies.
11. A replier runs or drops a copy marked as sent again. It never answers it with busy or with
    "no handler", because an older copy on the closed connection can still be read and run. It
    refuses another copy only while the connection that brought it is open. If that connection
    ended first, the replier sends nothing and forgets the id: it did not run the request.
12. One connection has at most one copy of a request that has no end. The requester sends a new
    copy after a busy end, or on a new connection. After it sends on a new connection, it reads
    nothing from the old one.
13. A requester may send a refused request again, with the same id and the same deadline, when
    every replier refused it. When only some refused, a retry cannot complete: a replier that
    already ran the request drops the repeat and sends no end.
14. Every process that sends a copy of one request reads the same clock. Two senders may differ by
    at most the keep time minus the handshake limit: 55 s with the defaults. The clock does not
    jump. One process with its own monotonic clock meets this rule. Several processes (several
    API replicas, or a restart that keeps open requests) need a clock they share.

### Replies lost in a reconnect

A replier that gets a known id, marked as sent again, tells its program the number of replies sent
and whether the end was sent. The program stores the replies: rulfo does not.

The program chooses one of two:

- Send the replies again: all of them, from number 0, in order, before any new reply. Then send
  the end again, if it was sent.
- Send nothing. If a reply or the end was lost, the outcome is `expired`.

The program keeps stored replies no longer than the replier keeps the id. It may keep fewer, for
example under a size limit.

A reply that arrives twice reaches the caller once (rule 3). The count in the end stays correct
(rule 5). A refused request has no stored id, so nothing is sent again for it.

### Clocks

A replier reads a deadline on its own clock. It uses the clock value that the requester sends once
on each connection (see [Wire format](wire-format.md#clocks-and-deadlines)). A copy gets the same
deadline however long it waits in a buffer.

That deadline is late by at most the handshake limit. The keep time is longer than the limit, so a
replier still has the id when it reads a copy that is not past its deadline.

Limits:

- rulfo does not check rule 14. Senders that differ by more can make a request run twice.
- With senders that differ, a replier can drop a copy shortly before the real deadline. The copy
  does not run, and the outcome is `expired`.
- A replier that restarts forgets its ids, and can run a copy again. Storing ids on disk is not
  designed yet.

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
