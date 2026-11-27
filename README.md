# rulfo

Messaging between processes where a request gets zero, one or many replies and always an end.
A Python package on a C core, for asyncio.

Status: early. Not on PyPI, and the API can change.

## Install

From a copy of the repository, with a C compiler:

```bash
uv pip install .
```

Linux with CPython 3.12 or later. Free-threaded Python is not supported.

## Quickstart

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

## Features

- Four outcomes: `completed`, `busy`, `expired`, `unroutable`. Two of them say it did not run.
- A deadline on every request, also when the two clocks differ.
- Zero, one or many replies, in order.
- A request survives a reconnect and runs once while the other process lives. Replies lost in
  the reconnect are sent again, up to a memory limit.
- Any byte stream: TCP and unix sockets included, WebSocket adapters in the docs.
- A model checks the rules, and the library replays runs of that model.

Not designed yet: latest value per key, work with acknowledgement, presence.

## Links

- [Python guide](https://cardona-oss.gitlab.io/rulfo/python/)
- [Operations](https://cardona-oss.gitlab.io/rulfo/operations/): the rules
- [Wire format](https://cardona-oss.gitlab.io/rulfo/wire-format/)
- [Model](https://cardona-oss.gitlab.io/rulfo/model/): what is proved and what is not
- [Contributing](https://gitlab.com/cardona-oss/rulfo/-/blob/main/CONTRIBUTING.md)

## License

MIT. See [LICENSE](https://gitlab.com/cardona-oss/rulfo/-/blob/main/LICENSE).
