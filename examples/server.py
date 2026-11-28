"""Quickstart server: answers the keys greet, count and slow.

Run: python examples/server.py [port]
Stop: Ctrl-C.
"""

import asyncio
import signal
import sys

import rulfo

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7373


async def greet(request: rulfo.Request) -> bytes:
    """One reply, then the end."""
    return b"hello " + request.body


async def count(request: rulfo.Request) -> None:
    """Many replies, then the end."""
    for number in range(int(request.body)):
        await request.reply(f"item {number}".encode())


async def slow(request: rulfo.Request) -> bytes:
    """Sleep for the seconds in the body, then reply."""
    print(f"[server] slow starts, repeat={request.repeat}", flush=True)
    await asyncio.sleep(float(request.body))
    return b"done"


server = rulfo.Server(handlers={"greet": greet, "count": count, "slow": slow})


async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Use one connection. Its first line is the name of the client: this example trusts it."""
    name = (await reader.readline()).decode().strip()
    print(f"[server] {name} connected", flush=True)
    try:
        await server.serve(name, rulfo.StreamTransport(reader, writer))
    except (OSError, rulfo.RulfoError) as error:
        print(f"[server] {name}: {error!r}", flush=True)
    print(f"[server] {name} disconnected", flush=True)


async def main() -> None:
    listener = await asyncio.start_server(accept, "127.0.0.1", PORT)
    print(f"[server] listening on port {PORT}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    await stop.wait()

    print("[server] stopping", flush=True)
    listener.close()  # no new connections
    await server.aclose()  # ends every connection and cancels the handlers
    await listener.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
