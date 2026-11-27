# Python

Use `rulfo.Peer` on asyncio to send requests and to answer them over one connection. The rules
behind it are in [Operations](operations.md), and every name is in the [API reference](api.md).

Linux with CPython 3.12 or later. Free-threaded Python is not supported. The wheel is for x86_64:
other machines build the package from source, with a C compiler.

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

The four outcomes are values. `result.one()` is the short form for one reply: it returns the body
and raises `NotCompleted` for another outcome, or `RemoteError` for an error reply.

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
must stay private. The `rulfo` logger records each one.

## Connections

`serve` uses one connection until it ends. Your program decides when to connect again:

```python
while True:
    try:
        reader, writer = await asyncio.open_connection(host, port)
        await peer.serve(rulfo.StreamTransport(reader, writer))
    except (OSError, rulfo.ProtocolError) as error:
        print(error)
    await asyncio.sleep(1)
```

- A request that was sent stays open across reconnects, until its deadline.
- Without a connection, `request` returns `unroutable` at once. `await peer.wait_connected()`
  waits for one.
- A second call of `serve` replaces the connection of the first.
- `await peer.aclose()` closes the connection, fails open requests with `PeerClosed` and cancels
  handlers. It does not send what still waits to be sent.

### One Peer for each remote party

A `Peer` remembers the ids it ran. A second `Peer` for the same party runs a repeated request
again. A server keeps one for each authenticated name:

```python
peers: dict[str, rulfo.Peer] = {}

if name not in peers:
    peers[name] = rulfo.Peer(handlers=handlers)
await peers[name].serve(transport)
```

Drop a `Peer` only when `peer.idle` is true and no connection serves it.

## Transports

A transport has `send`, `recv` and `close` (see `rulfo.Transport`). `send` gets at most 64 KiB
at a time, and the cuts between chunks have no meaning.

- TCP and unix sockets: `rulfo.StreamTransport`.
- Tests: `rulfo.testing.memory_pair()`.
- WebSockets: copy one of the two adapters below. They were run once, with Litestar 2.25.0 and
  websockets 17.4. The tests of rulfo do not run them.

rulfo does not encrypt or authenticate. Use a transport that does.

### Litestar (server side)

```python
import contextlib

from litestar import WebSocket, websocket
from litestar.exceptions import WebSocketDisconnect, WebSocketException


class LitestarTransport:
    """Call ``await socket.accept()`` before ``peer.serve``."""

    def __init__(self, socket: WebSocket) -> None:
        self.socket = socket

    async def send(self, data: bytes) -> None:
        with contextlib.suppress(WebSocketDisconnect):  # recv reports the end
            await self.socket.send_bytes(data)

    async def recv(self) -> bytes:
        try:
            return await self.socket.receive_bytes()
        except WebSocketDisconnect:
            return b""

    async def close(self) -> None:
        with contextlib.suppress(WebSocketDisconnect, WebSocketException):
            await self.socket.close()


@websocket("/rulfo/{name:str}")
async def endpoint(socket: WebSocket, name: str) -> None:
    await socket.accept()
    # Check who is calling here: one Peer for each authenticated name.
    if name not in peers:
        peers[name] = rulfo.Peer()
    await peers[name].serve(LitestarTransport(socket))
```

Both sides send binary messages only. A text message or an empty one ends the connection.

### websockets (client side)

```python
import contextlib

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed


class WebsocketsTransport:
    def __init__(self, socket: ClientConnection) -> None:
        self.socket = socket

    async def send(self, data: bytes) -> None:
        with contextlib.suppress(ConnectionClosed):  # recv reports the end
            await self.socket.send(data)

    async def recv(self) -> bytes:
        try:
            return await self.socket.recv(decode=False)
        except ConnectionClosed:
            return b""

    async def close(self) -> None:
        await self.socket.close()


async for socket in connect(url):
    await peer.serve(WebsocketsTransport(socket))
```

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

Processes that send copies of one request id need one clock (rule 14 in
[Operations](operations.md)). `Peer(clock=...)` takes it, in milliseconds. `Peer.request` always
uses a new id, so one process needs nothing.

## Without asyncio

`rulfo.PeerState` is the same logic without I/O and without a clock: you pass the bytes and the
time. Use it for another event loop. `Peer` is built on it.
