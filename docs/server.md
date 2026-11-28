# Server

Use `rulfo.Server` when many clients connect to one process. It keeps one `Peer` for each
client, found by a name. Your program accepts each connection, finds out who is calling, and
passes the connection to `serve` with that name.

The blocks on this page are parts of
[`examples/auth_server.py`](https://gitlab.com/cardona-oss/rulfo/-/blob/main/examples/auth_server.py).
[Run the example](#run-the-example) has the commands and the output.

## Check who is calling

rulfo has no authentication. Whoever gives a name gets the requests you send to that name, so
check the name before `serve`.

In the example, the first line of a connection is `name token`. `check_caller` reads that line
from the same reader that rulfo uses afterwards, so rulfo starts at the next byte:

```python
--8<-- "examples/auth_server.py:check"
```

- `check_caller` has a time limit. Without one, a client that sends nothing holds a connection
  open.
- `hmac.compare_digest` compares the token in a time that does not depend on where it differs.
- `Request.caller` is the name you passed to `serve`. A handler reads it to know who asks.
- The `Peer` of a name stays across reconnects, so a request that is sent again does not run
  twice.
- A second connection with the same name replaces the first. The first call of `serve` returns.

## Send a request to a client

`server.request` takes the name first:

```python
--8<-- "examples/auth_server.py:request"
```

- `server.connected` is a tuple of the names a request can be sent to now, sorted.
- A name with no connection gives `unroutable`, as `worker-9` here.
- A name whose connection is in the handshake makes the call wait.

## TLS

rulfo does not encrypt. Give `asyncio.start_server` an `ssl.SSLContext`, and `StreamTransport`
works on the encrypted stream with no other change:

```python
--8<-- "examples/auth_server.py:tls"
```

The server passes it as `asyncio.start_server(accept, "127.0.0.1", PORT, ssl=tls_context())`.
The client passes its own context to `asyncio.open_connection(..., ssl=...)`: see `tls_context`
in `examples/worker.py` below.

To try it with a certificate that you sign yourself:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
  -addext subjectAltName=IP:127.0.0.1 -keyout server.key -out server.crt
RULFO_CERT=server.crt RULFO_KEY=server.key python examples/auth_server.py
RULFO_CA=server.crt python examples/worker.py worker-1 token-1
RULFO_CA=server.crt python examples/worker.py worker-2 token-2
```

The output is the same as without TLS. Each command needs its own terminal.

## Memory for each client

`config` applies to every `Peer` of the server. Its limits count for each client, not in total:
with the defaults, each client can have 64 handlers running and 16 MiB of stored replies.

A `Peer` allocates memory for `max_ids` ids when it is made. Measured on Linux x86_64:

| `max_ids` | Memory for each client |
|---|---|
| 4096 (default) | about 235 KB |
| 256 | about 19 KB |

```python
--8<-- "examples/auth_server.py:config"
```

`max_ids` also limits the rate of new requests from one client: see
[Limits and waiting](python.md#limits-and-waiting).

The server drops the `Peer` of a name that has no connection, no open request, no running
handler and no kept id. It looks for them each time a connection arrives. A name keeps its ids
for `keep` (60 s) after the deadline of its last request.

## Shut down

Stop in this order:

```python
--8<-- "examples/auth_server.py:shutdown"
```

1. `listener.close()` stops new connections.
2. `server.aclose()` closes every connection, fails the open requests with `PeerClosed` and
   cancels the handlers. Every call of `serve` returns.
3. `listener.wait_closed()` waits until every call of `accept` returned. Before step 2 it waits
   until every client goes away.

After `aclose`, `request`, `request_copy` and `serve` raise `PeerClosed`. If the call of `aclose`
is cancelled, call it again.

Nothing waits for the handlers to finish their work. A requester keeps a request that was
running open. It ends `expired` at its deadline, or it runs again on a server that the requester
reaches before then, as in the
[Quickstart](quickstart.md#stop-the-server-during-a-request).

## What Server does not have

- No listener. Your program calls `asyncio.start_server` or its web framework.
- No authentication and no encryption. See the two sections above.
- No call that waits for a name to connect. Look at `server.connected`, as the example does.
  `request_copy` with `sent_before=True` waits for the name: see
  [A second process takes over](python.md#a-second-process-takes-over).
- No callback when a client connects or disconnects. Your `accept` function knows both: it
  runs before `serve` and after `serve` returns.
- No request to all clients. Loop over `server.connected` and call `request` for each name.
- No list of the names that are away. `connected` has only the names with a connection.
- No limit on the number of clients. Count them in `accept` if you need one.
- No state outside the process. Two server processes do not share clients or ids.

## Run the example

`examples/worker.py` is the client. It sends its name and token, answers `resize`, and sends
one request to the server:

```python
--8<-- "examples/worker.py"
```

1. Start the server in terminal 1. It waits for two workers:

    ```bash
    python examples/auth_server.py
    ```

2. Try a wrong token in terminal 2:

    ```bash
    python examples/worker.py worker-1 wrong
    ```

3. Start the first worker in terminal 2:

    ```bash
    python examples/worker.py worker-1 token-1
    ```

4. Start the second worker in terminal 3:

    ```bash
    python examples/worker.py worker-2 token-2
    ```

The worker with the wrong token prints:

```text
[worker-1] the server refused the connection
```

The server prints, and then stops:

```text
[server] listening on port 7374
[server] refused a connection
[server] worker-1 passed the check
[server] worker-2 passed the check
[server] worker-1: completed b'worker-1 resized to 640x480'
[server] worker-2: completed b'worker-2 resized to 640x480'
[server] worker-9: unroutable
[server] stopped
```

Then worker-1 prints:

```text
[worker-1] who: b'you are worker-1'
[worker-1] the server closed the connection
```
