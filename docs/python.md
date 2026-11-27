# Python

Use `rulfo.Peer` on asyncio to send requests and to answer them over one connection. The rules
behind it are in [Operations](operations.md), and every name is in the [API reference](api.md).

Linux and macOS with CPython 3.12 or later. Free-threaded Python is not supported. There are
wheels for Linux (x86_64 and aarch64) and macOS: other machines build the package from source, with a C compiler.

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
again. A server with several clients uses `rulfo.Server`, which keeps one `Peer` for each name:

```python
async def who(request: rulfo.Request) -> bytes:
    return request.caller.encode()


server = rulfo.Server(handlers={"who": who})


async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    name = await check_caller(reader)  # your code: who is calling
    await server.serve(name, rulfo.StreamTransport(reader, writer))


await asyncio.start_server(accept, host, port)
```

Send a request to one client by its name:

```python
result = await server.request("worker-1", "resize", body, timeout=5)
```

- Pass a name that you checked. Whoever gives a name gets its requests.
- A second connection with the same name replaces the first.
- `server.connected` lists the names a request can be sent to now. For a name with no connection
  the outcome is `unroutable`. During the handshake of a connection the call waits.
- A `Peer` with no connection stays while it has something to remember. It is dropped when the next
  connection arrives after that.
- Each name has its own `Peer`: memory and the limits of `Config` count for each one.
- `await server.aclose()` closes every `Peer`.

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


server = rulfo.Server()


@websocket("/rulfo/{name:str}")
async def endpoint(socket: WebSocket, name: str) -> None:
    await socket.accept()
    # Check here that the caller may use this name.
    await server.serve(name, LitestarTransport(socket))
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

### A second process takes over

`Peer.request` makes a new id each time. To let another process finish a request, choose the id and
the deadline yourself, store them, and send them with `request_copy`:

```python
request_id = os.urandom(16)
deadline_ms = clock() + 30_000
await store(request_id, "resize", body, deadline_ms)  # your code
result = await peer.request_copy(
    request_id, "resize", body, deadline_ms=deadline_ms, sent_before=False
)
```

The process that takes over passes the same values, with `sent_before=True`:

```python
result = await peer.request_copy(
    request_id, "resize", body, deadline_ms=deadline_ms, sent_before=True
)
```

A `Server` has the same call with the name first. With `sent_before=True` it also waits for a
client that has not connected yet:

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
- All processes need one clock, in milliseconds (rule 14 in [Operations](operations.md)). Pass it
  as `Peer(clock=...)` or `Server(clock=...)`. The default clock belongs to one machine.
- Do not send an id again after you cancelled its call.

## Without asyncio

`rulfo.PeerState` is the same logic without I/O and without a clock: you pass the bytes and the
time. Use it for another event loop. `Peer` is built on it.
