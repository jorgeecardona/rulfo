# Peer

Use `rulfo.Peer` on asyncio to send requests and to answer them over one connection. The rules
behind it are in [Operations](operations.md), and every name is in the [API reference](api.md).

Linux and macOS with CPython 3.12 or later. Free-threaded Python is not supported. There are
wheels for Linux (x86_64 and aarch64) and macOS. Other machines build the package from source,
with a C compiler.

Blocks with a file name above them are complete programs from
[`examples/`](https://gitlab.com/cardona-oss/rulfo/-/tree/main/examples): a test runs each one.
The other blocks are fragments. They go inside an `async def`, after `import rulfo`, with `peer`
a connected `rulfo.Peer` as in the [Quickstart](quickstart.md).

## Send a request

```python
result = await peer.request("greet", b"world", timeout=5)
if result.outcome is rulfo.Outcome.COMPLETED:
    for reply in result.replies:
        print(reply.body, reply.error)
```

`timeout` has no default: every request has a deadline. `request` returns when it has an outcome.

| Outcome | What happened | Safe to send as a new request |
|---|---|---|
| `completed` | The handler ran and every reply arrived. A reply can be an error reply. | It ran. |
| `busy` | The other side refused it. | Yes: it did not run. |
| `unroutable` | No handler for the key, or no connection. | Yes: it did not run. |
| `expired` | The deadline passed first. | No: it may have run. |

The four outcomes are values, not exceptions. `result.one()` is the short form for one reply: it
returns the body. It raises `NotCompleted` for another outcome, `RemoteError` for an error reply,
and `ValueError` for no reply or more than one. [Errors](errors.md) lists every exception.

## Answer requests

A handler is an async function for one key:

```python
async def greet(request: rulfo.Request) -> bytes:
    return b"hello " + request.body


async def count(request: rulfo.Request) -> None:
    for number in range(int(request.body)):
        await request.reply(f"item {number}".encode())


peer = rulfo.Peer(handlers={"greet": greet, "count": count})
```

| The handler | The requester gets |
|---|---|
| returns bytes | one reply, then `completed` |
| returns None | `completed` after the replies it sent |
| raises `rulfo.Busy` before its first reply | `busy` |
| raises another exception | an error reply with the type and message, then `completed` |
| is still running at the deadline | `expired`; the handler is cancelled |

The error reply carries the text of the exception to the other side. Catch exceptions whose text
must stay private. The `rulfo` logger records each one with its traceback.

`examples/outcomes.py` has one handler for each row, and sends a request to each:

```python
--8<-- "examples/outcomes.py"
```

It prints:

```text
Before the connection
greet: unroutable []
Connected
greet: completed [(b'hello world', False)]
count: completed [(b'item 0', False), (b'item 1', False), (b'item 2', False)]
count: completed []
full: busy []
unknown: unroutable []
slow: expired []
broken: completed [(b"ValueError: cannot read b'x'", True)]
one() raises RemoteError: ValueError: cannot read b'x'
```

Each pair is the body of a reply and whether it is an error reply. The traceback of `broken`
goes to the `rulfo` logger, which prints on stderr here.

## Connections

`serve` uses one connection until it ends. Your program decides when to connect again. This loop
is from `examples/client.py`, where it runs as a task:

```python
--8<-- "examples/client.py:reconnect"
```

- A request that was sent stays open across reconnects, until its deadline.
- Without a connection, `request` returns `unroutable` at once. `await peer.wait_connected()`
  waits for one.
- A second call of `serve` replaces the connection of the first.
- `serve` returns when the other side closes the connection. It raises `ProtocolError` or
  `Overloaded` when rulfo ends it, and what the transport raises, such as `OSError`.
- `await peer.aclose()` closes the connection, fails open requests with `PeerClosed` and cancels
  handlers. It does not send what still waits to be sent.
- After `aclose`, a call of `serve` that runs returns, and the next call raises `PeerClosed`.
  Cancel the task of the loop, as `examples/client.py` does, or the loop sleeps and connects
  once more before it ends.

A `Peer` remembers the ids it ran, so keep one `Peer` for each remote party, also across
reconnects. A second `Peer` for the same party runs a repeated request again. A process with
many clients uses `rulfo.Server`, which keeps one `Peer` for each name: see [Server](server.md).

What a connection can be (TCP, unix sockets, WebSockets, your own) is in
[Transports](transports.md). rulfo does not encrypt or authenticate: use a transport that does.

## Cancel

- Cancelling the task that awaits `request` stops the wait. Nothing is sent: the handler on the
  other side runs to its end or to the deadline.
- A disconnect cancels no handler. Its replies are stored and sent when the requester asks again.
- A handler is cancelled at its deadline and by `aclose`. No end is sent for it.

## Limits and waiting

| Setting of `rulfo.Config` | Default | At the limit |
|---|---|---|
| `max_requests` | 64 | `request` waits for a free place, until its deadline |
| `send_buffer` | 1 MiB | `request` and `reply` wait until the connection takes more |
| `max_running` | 64 | a new request is refused as `busy` |
| `max_ids` | 4096 | a new request is refused as `busy` |
| `max_deadline` | 1 hour | a request with a later deadline is refused as `busy` |
| `max_length` | 16 MiB | sending raises `ValueError`; receiving closes the connection |
| `replay_bytes_per_request` | 1 MiB | the replies of that request are not stored |
| `replay_bytes` | 16 MiB | the replies of the request that goes over are not stored |
| `keep` | 60 s | how long an id is kept after its deadline |
| `handshake` | 5 s | a connection that is not ready by then raises `ProtocolError` |

Times are in seconds. `max_ids` limits the rate of new requests to
`max_ids / (deadline + keep)` per second: 58 per second with 10 s deadlines.

`max_deadline`, `max_ids` and `max_running` limit what this side answers. Set `max_length` to
the same value on both sides.

A request whose replies are not stored ends `expired` if a reconnect loses one of them.

## Memory

- Requester: the key and body of each open request, and all its replies until the outcome.
  `request` has no way yet to read replies one at a time.
- Replier: stored replies, up to `replay_bytes`, until the deadline of their request.
- Unsent bytes: a connection whose other side stops reading ends with `rulfo.Overloaded`. The
  limit is four times `send_buffer`, plus `max_length`, `replay_bytes` and the open requests:
  36 MiB and more with the defaults.
- Each `Peer` allocates about 235 KB with the default `max_ids`. A side that only sends requests
  can set `max_ids=1`.

## Restarts and several processes

A restarted process forgets the ids it ran. A requester that still waits sends its request again,
and it runs again. `request.repeat` is true in that case: a handler that must not run twice checks
its own record when it is true.

### A second process takes over

`Peer.request` makes a new id each time. To let another process finish a request, choose the id
and the deadline yourself, store them, and send them with `request_copy`.

All the processes read one clock, in milliseconds (rule 14 in [Operations](operations.md)). The
default clock of a `Peer` belongs to one machine and starts at its boot. For processes on one
machine, the wall clock works while nobody sets it back:

```python
--8<-- "examples/takeover.py:clock"
```

The first process stores the request before it sends it, with `sent_before=False`. These
fragments are from `examples/takeover.py`, a complete program where two `Peer` objects stand
for the two processes:

```python
--8<-- "examples/takeover.py:first"
```

The process that takes over passes the same values, with `sent_before=True`:

```python
--8<-- "examples/takeover.py:second"
```

The whole example prints:

```text
[worker] resize runs, repeat=False
[api 1] stopped before the reply
[api 2] completed b'resized 640x480'
[worker] the handler ran 1 time
```

A `Server` has the same call with the name first. With `sent_before=True` it also waits for a
client that has not connected yet. This fragment needs the `request_id` and `deadline_ms` that
you stored:

```python
result = await server.request_copy(
    "worker-1", request_id, "resize", body, deadline_ms=deadline_ms, sent_before=True
)
```

- The other side runs the request only if it does not know the id. If it knows the id, it sends
  the stored replies again.
- With `sent_before=True` the outcome is never `unroutable` or `busy`. Without a connection the
  call waits. If the other side has no handler or is at a limit, the outcome is `expired` at the
  deadline.
- Delete the stored request before you report its outcome. If another process can pick it up at
  any time, pass `sent_before=True` in the first call too: otherwise the first process can report
  `unroutable` and the second one still runs the request.
- All processes must reach the same `Peer` on the other side. A `Server` there needs one name for
  all of them, and only one of them connected at a time: a second connection with a name replaces
  the first.
- Pass the clock as `Peer(clock=...)` or `Server(clock=...)`. Two senders may differ by at most
  `keep` minus `handshake`: 55 s with the defaults. rulfo does not check this. Senders that
  differ by more, or a clock that jumps, can make a request run twice.
- Do not send an id again after you cancelled its call.

## Without asyncio

`rulfo.PeerState` is the same logic without I/O and without a clock: you pass the bytes and the
time. Use it for another event loop. `Peer` is built on it.
