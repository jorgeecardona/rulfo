# Transports

A transport is one connection that `Peer.serve` or `Server.serve` uses. Pick one from this
table, or write your own.

| Connection | Transport | Tested by rulfo |
|---|---|---|
| TCP | `rulfo.StreamTransport` | Yes |
| Unix socket | `rulfo.StreamTransport` | Yes |
| TCP with TLS | `rulfo.StreamTransport` | Yes: see [Server](server.md#tls) |
| Memory, for tests | `rulfo.testing.memory_pair()` | Yes: see [Testing](testing.md) |
| WebSocket, Litestar | `LitestarTransport`, a class to copy from this page | Yes, as an example |
| WebSocket, websockets library | `WebsocketsTransport`, a class to copy from this page | Yes, as an example |

rulfo does not encrypt or authenticate. Use TLS, and check who is calling before you call
`serve`: [Server](server.md) shows both.

## TCP and unix sockets

`rulfo.StreamTransport` takes the reader and the writer of an asyncio stream. The
[Quickstart](quickstart.md) uses it over TCP.

For a unix socket, change the two asyncio calls. These two lines are fragments: `accept` and
`path` are yours.

```python
listener = await asyncio.start_unix_server(accept, path)  # in the server
reader, writer = await asyncio.open_unix_connection(path)  # in the client
```

## Write your own

A transport is any object with these three methods (`rulfo.Transport`). It needs no base class.

| Method | What it does |
|---|---|
| `async send(data: bytes) -> None` | Sends all of `data`. Returns when the connection can take more. |
| `async recv() -> bytes` | Returns the next bytes that arrived, or `b""` when the connection has ended. |
| `async close() -> None` | Closes the connection. |

- Order: the bytes arrive complete and in order. rulfo does not repair a transport that loses
  or reorders them.
- Size: `send` gets at most 64 KiB in one call. `recv` can return any number of bytes: where one
  chunk ends has no meaning.
- Waiting: `send` returns only when the connection can take more. That is how `request` and
  `reply` slow down for a slow reader.
- End: `recv` returns `b""` when the other side closed. Then `serve` returns.
- Errors: what `send` or `recv` raises ends `serve`, and `serve` raises it. A `send` on a
  connection that has ended can return without an error: `recv` reports the end.
- Close: `serve` calls `close` once, when it ends for any reason.

The two WebSocket classes below are complete transports of about 20 lines.

## WebSockets

A WebSocket message is one chunk of bytes for rulfo. Send only binary messages. A text message
or an empty message ends the connection.

The two files below are one program each. The client talks to the server. They ran with
Litestar 2.25.0, uvicorn 0.54.0 and websockets 17.4.

### Litestar, the server side

`examples/litestar_server.py`:

```python
--8<-- "examples/litestar_server.py"
```

### websockets, the client side

`examples/websocket_client.py`:

```python
--8<-- "examples/websocket_client.py"
```

### Run them

1. Install what they need:

    ```bash
    pip install rulfo litestar uvicorn websockets
    ```

2. Start the server in terminal 1:

    ```bash
    uvicorn --app-dir examples litestar_server:app --port 8000
    ```

3. Run the client in terminal 2:

    ```bash
    python examples/websocket_client.py
    ```

The client prints:

```text
greet: completed b'hello world, you are client-1'
```

## A WebSocket that also has text messages

Use this when one WebSocket already has a protocol of text messages (JSON, for example) and
you want rulfo on the same socket. rulfo gets the binary messages.

The rule: one task reads the socket. It gives each binary message to the transport, and handles
each text message itself. The transport never reads the socket.

- Do not call `socket.recv(decode=False)` in the transport. With the websockets library it
  returns a text message as bytes, and rulfo ends the connection on bytes it does not know.
- Do not wait for `serve` in the task that reads. `serve` returns at the end of the connection,
  and until then nobody would read the socket. Run `serve` as its own task.

The transport, from `examples/shared_websocket.py`:

```python
--8<-- "examples/shared_websocket.py:transport"
```

The reader. Both sides run it:

```python
--8<-- "examples/shared_websocket.py:reader"
```

One side sends the text message `START` and then starts `serve`. The other side starts `serve`
when `START` arrives. Messages arrive in order, so every binary message after `START` is for
rulfo:

```python
--8<-- "examples/shared_websocket.py:start"
```

The whole file is a server and a client in one process:

```bash
pip install rulfo websockets
python examples/shared_websocket.py
```

It prints:

```text
[server] text: {"type": "chat", "text": "before rulfo"}
[client] greet: completed b'hello world'
[server] text: {"type": "chat", "text": "between two requests"}
[client] greet: completed b'hello again'
[server] text: {"type": "chat", "text": "after rulfo"}
```

Limits of this example:

- rulfo starts once on each WebSocket. To start it again on the same socket, both sides must
  agree in each direction on the message where the new stream starts. The example does not do
  that: open a new WebSocket instead.
- `feed` does not wait, and its queue has no limit: the binary messages that rulfo has not read
  yet stay in memory.
