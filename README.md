<h1 align="center">rulfo</h1>

<p align="center"><strong>Request and reply between processes for asyncio: every request has a deadline, gets zero, one or many replies, and ends with one of four outcomes.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/rulfo/"><img alt="PyPI" src="https://img.shields.io/pypi/v/rulfo?style=flat-square"></a>
  <a href="https://pypi.org/project/rulfo/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/rulfo?style=flat-square"></a>
  <a href="https://cardona-oss.gitlab.io/rulfo/"><img alt="Docs" src="https://img.shields.io/badge/docs-cardona--oss.gitlab.io%2Frulfo-blue?style=flat-square"></a>
  <a href="https://gitlab.com/cardona-oss/rulfo/-/pipelines"><img alt="Pipeline" src="https://img.shields.io/gitlab/pipeline-status/cardona-oss%2Frulfo?branch=main&style=flat-square"></a>
  <a href="https://gitlab.com/cardona-oss/rulfo/-/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square"></a>
</p>

<p align="center">
  <a href="https://cardona-oss.gitlab.io/rulfo/python/">Python guide</a> ·
  <a href="https://cardona-oss.gitlab.io/rulfo/operations/">Operations</a> ·
  <a href="https://cardona-oss.gitlab.io/rulfo/model/">Model</a> ·
  <a href="https://cardona-oss.gitlab.io/rulfo/api/">API reference</a> ·
  <a href="https://gitlab.com/cardona-oss/rulfo/-/blob/main/CHANGELOG.md">Changelog</a>
</p>

---

## Overview

rulfo is a Python package on a C core. Two processes share one connection, and each side can
send requests and answer them. A request has a key, a body of bytes and a deadline. It gets zero,
one or many replies, in order, and then one outcome: `completed`, `busy`, `expired` or
`unroutable`.

Use it between two services, or between a service and its workers, when the caller must know
what happened to each request: it ran, it did not run, or that is unknown.

Status: early. Only the `request` operation is built, and the API can change.

## Install

```bash
pip install rulfo      # or: uv add rulfo
```

Linux with CPython 3.12 or later. Free-threaded Python is not supported. The wheel is for x86_64.
Other machines build the package from source, with a C compiler and the Python headers
(`python3-dev` for a Debian system Python).

## Quickstart

One process here plays both sides: a server with two handlers and a client that sends three
requests over TCP.

```python
import asyncio

import rulfo


async def greet(request: rulfo.Request) -> bytes:
    return b"hello " + request.body  # one reply, then the end


async def count(request: rulfo.Request) -> None:
    for number in range(int(request.body)):
        await request.reply(f"item {number}".encode())  # many replies, then the end


async def main() -> None:
    # One Peer for each remote party. This server has one client.
    server = rulfo.Peer(handlers={"greet": greet, "count": count})

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await server.serve(rulfo.StreamTransport(reader, writer))

    listener = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]

    client = rulfo.Peer()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    connection = asyncio.create_task(client.serve(rulfo.StreamTransport(reader, writer)))
    await client.wait_connected()

    greeting = await client.request("greet", b"world", timeout=5)
    print(greeting.outcome.value, greeting.one())

    numbers = await client.request("count", b"3", timeout=5)
    print(numbers.outcome.value, [reply.body for reply in numbers.replies])

    missing = await client.request("unknown", timeout=5)
    print(missing.outcome.value)

    await client.aclose()
    await server.aclose()
    await connection
    listener.close()
    await listener.wait_closed()


asyncio.run(main())
```

Output:

```text
completed b'hello world'
completed [b'item 0', b'item 1', b'item 2']
unroutable
```

## Outcomes

`request` returns one of four outcomes. `timeout` has no default: every request has a deadline.

| Outcome | What happened | Did the handler run |
|---|---|---|
| `completed` | The handler ran and every reply arrived. A reply can be an error reply. | Yes |
| `busy` | The other side refused the request. | No |
| `unroutable` | No handler for the key, or no connection. | No |
| `expired` | The deadline passed first. | Unknown |

A `busy` or `unroutable` request is safe to send as a new request. An `expired` one may have
run. The rules are in [Operations](https://cardona-oss.gitlab.io/rulfo/operations/).

## Why rulfo

HTTP: a call that times out, or whose connection drops, does not say whether the handler ran.
A retry can run it twice unless you add your own request ids. Many replies need a stream, and
resuming a stream that dropped is your code.

ZeroMQ: it gives you sockets and patterns. Its
[guide](https://zguide.zeromq.org/docs/chapter4/) builds reliable request and reply as patterns
in your own program: retries, and detecting a request that arrives twice.

rulfo has these rules built in, for request and reply only:

- The requester sends an open request again after a reconnect, with the same id. The replier
  runs one id at most once while its process lives.
- The end of a request has the number of replies. `completed` means that all of them arrived.
- A request that the replier refuses ends `busy`. It did not run.

Use something else when you need publish and subscribe, work queues, a broker, discovery, a
schema, or a language other than Python. rulfo has none of them.

## Key features

- Four outcomes: `completed`, `busy`, `expired`, `unroutable`. Two of them say it did not run.
- A deadline on every request, also when the clocks of the two processes differ.
- Zero, one or many replies, in order.
- A request stays open across a reconnect. Replies lost in the reconnect are sent again, up to
  a memory limit.
- Limits with a stated result: a replier at `max_running` refuses with `busy`.
- Any ordered byte stream: TCP and unix sockets with `rulfo.StreamTransport`, and a memory
  transport for tests. Two WebSocket adapters to copy are in the
  [Python guide](https://cardona-oss.gitlab.io/rulfo/python/#transports).
- `rulfo.PeerState`: the same logic without I/O and without a clock, for another event loop.
- The rules are checked with a [musil](https://pypi.org/project/musil/) model. A test replays
  12000 random runs of that model on the library.

## Limits

- Only `request` is built. Latest value per key, work with acknowledgement and presence are not
  designed yet.
- A `Peer` serves one remote party over one connection. A server keeps one `Peer` for each
  client.
- rulfo does not encrypt or authenticate. Use a transport that does.
- The body is bytes. rulfo has no serialization and no schema.
- A replier that restarts forgets the ids it ran. A request that is still open runs again, and
  the handler sees `request.repeat` as true.
- `request` keeps all replies in memory until the outcome.
- Cancelling the task that awaits `request` does not stop the handler on the other side.
- The model covers small cases only. [Model](https://cardona-oss.gitlab.io/rulfo/model/) lists what it proves and what it does
  not.

## Links

- Documentation: <https://cardona-oss.gitlab.io/rulfo/>
- [Wire format](https://cardona-oss.gitlab.io/rulfo/wire-format/): the bytes, version 1
- Repository: <https://gitlab.com/cardona-oss/rulfo>
- PyPI: <https://pypi.org/project/rulfo/>
- Changelog: [CHANGELOG.md](https://gitlab.com/cardona-oss/rulfo/-/blob/main/CHANGELOG.md)

## Contributing

The build needs a C compiler, and the C tests need cmake. The toolchain, the checks and the
release process are in
[CONTRIBUTING.md](https://gitlab.com/cardona-oss/rulfo/-/blob/main/CONTRIBUTING.md).

## License

MIT © Jorge Cardona. See [LICENSE](https://gitlab.com/cardona-oss/rulfo/-/blob/main/LICENSE).
