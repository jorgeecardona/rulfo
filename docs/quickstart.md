# Quickstart

Run a server and a client as two processes over TCP. Then stop the server during a request and
see what the client gets.

You need `pip install rulfo` and two files. The package does not contain them. Make a folder
named `examples/` and save the two files below in it, or clone the
[repository](https://gitlab.com/cardona-oss/rulfo/-/tree/main/examples).

## The server

`examples/server.py` answers three keys. Each connection starts with one line, the name of the
client, and then rulfo uses the connection.

```python
--8<-- "examples/server.py"
```

This server trusts the name. [Server](server.md) shows one that checks a token.

## The client

`examples/client.py` connects, sends three requests and exits.

```python
--8<-- "examples/client.py"
```

## Run it

1. Start the server in terminal 1:

    ```bash
    python examples/server.py
    ```

2. Run the client in terminal 2:

    ```bash
    python examples/client.py
    ```

3. Stop the server with Ctrl-C.

Terminal 2 prints:

```text
greet: completed b'hello world'
count: completed [b'item 0', b'item 1', b'item 2']
unknown: unroutable
```

Terminal 1 prints:

```text
[server] listening on port 7373
[server] client-1 connected
[server] client-1 disconnected
[server] stopping
```

Both programs take another port as their first argument: `python examples/server.py 7400`.

## Stop the server during a request

[`examples/slow_client.py`](https://gitlab.com/cardona-oss/rulfo/-/blob/main/examples/slow_client.py)
sends one request to the key `slow`. The handler takes 3 seconds and
the deadline is 10 seconds. The file is `client.py` with another request:

```python
--8<-- "examples/slow_client.py:request"
```

1. Start the server: `python examples/server.py`.
2. Start the client: `python examples/slow_client.py`.
3. Stop the server with Ctrl-C when it prints `slow starts`.
4. Start the server again within a few seconds.

The first server printed:

```text
[server] listening on port 7373
[server] client-1 connected
[server] slow starts, repeat=False
[server] stopping
[server] client-1 disconnected
```

The client connects again each second. When the server is back, it sends the request again with
the same id, and the request completes:

```text
[client] connected
[client] no connection: [Errno 111] Connect call failed ('127.0.0.1', 7373)
[client] no connection: [Errno 111] Connect call failed ('127.0.0.1', 7373)
[client] connected
slow: completed [b'done']
```

The second server prints:

```text
[server] listening on port 7373
[server] client-1 connected
[server] slow starts, repeat=True
[server] client-1 disconnected
[server] stopping
```

The handler ran twice. A server that restarts forgets the ids it ran, so it runs the request
again, and `request.repeat` is true to say so. A handler that must not run twice checks its own
record when `repeat` is true.

If only the connection drops and the server process lives, the handler goes on and runs once.
The server stores its reply and sends it when the client asks again.

Do not start the server again, and the request ends `expired` at its deadline. `expired` means
the request may have run: here it started.

```text
[client] connected
[client] no connection: [Errno 111] Connect call failed ('127.0.0.1', 7373)
[client] no connection: [Errno 111] Connect call failed ('127.0.0.1', 7373)
[client] no connection: [Errno 111] Connect call failed ('127.0.0.1', 7373)
[client] no connection: [Errno 111] Connect call failed ('127.0.0.1', 7373)
[client] no connection: [Errno 111] Connect call failed ('127.0.0.1', 7373)
[client] no connection: [Errno 111] Connect call failed ('127.0.0.1', 7373)
[client] no connection: [Errno 111] Connect call failed ('127.0.0.1', 7373)
[client] no connection: [Errno 111] Connect call failed ('127.0.0.1', 7373)
slow: expired []
```

The number of `no connection` lines depends on how long the server is away. The text of the
error is the one Linux gives.

## Next

- [Peer](python.md): outcomes, handlers, the reconnect loop, limits.
- [Server](server.md): many clients, a token check, TLS, shutdown.
- [Transports](transports.md): TCP, unix sockets, WebSockets, your own.
- [Errors](errors.md): every exception and what to do.
- [Testing](testing.md): test your handlers without a socket.
