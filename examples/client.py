"""Quickstart client: sends three requests to examples/server.py and exits.

Run: python examples/client.py [port]
"""

import asyncio
import contextlib
import sys

import rulfo

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7373


async def keep_connected(peer: rulfo.Peer) -> None:
    """Connect, use the connection until it ends, wait one second, connect again."""
    # --8<-- [start:reconnect]
    while True:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
            writer.write(b"client-1\n")  # examples/server.py reads a name first
            await peer.serve(rulfo.StreamTransport(reader, writer))
        except (OSError, rulfo.ProtocolError, rulfo.Overloaded) as error:
            print(f"[client] no connection: {error}", flush=True)
        await asyncio.sleep(1)
    # --8<-- [end:reconnect]


async def main() -> None:
    peer = rulfo.Peer()
    connection = asyncio.create_task(keep_connected(peer))
    await peer.wait_connected()

    result = await peer.request("greet", b"world", timeout=5)
    print("greet:", result.outcome.value, result.one())

    result = await peer.request("count", b"3", timeout=5)
    print("count:", result.outcome.value, [reply.body for reply in result.replies])

    result = await peer.request("unknown", timeout=5)
    print("unknown:", result.outcome.value)

    await peer.aclose()
    connection.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await connection


if __name__ == "__main__":
    asyncio.run(main())
