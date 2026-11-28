"""Sends one request that takes 3 seconds to examples/server.py, with a deadline of 10 seconds.

Run: python examples/slow_client.py [port]
Stop and start the server while it waits.
"""

import asyncio
import contextlib
import sys

import rulfo

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7373


async def keep_connected(peer: rulfo.Peer) -> None:
    """Connect, use the connection until it ends, wait one second, connect again."""
    while True:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
            writer.write(b"client-1\n")
            print("[client] connected", flush=True)
            await peer.serve(rulfo.StreamTransport(reader, writer))
        except (OSError, rulfo.ProtocolError, rulfo.Overloaded) as error:
            print(f"[client] no connection: {error}", flush=True)
        await asyncio.sleep(1)


async def main() -> None:
    peer = rulfo.Peer()
    connection = asyncio.create_task(keep_connected(peer))
    await peer.wait_connected()

    # --8<-- [start:request]
    result = await peer.request("slow", b"3", timeout=10)
    print("slow:", result.outcome.value, [reply.body for reply in result.replies], flush=True)
    # --8<-- [end:request]

    await peer.aclose()
    connection.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await connection


if __name__ == "__main__":
    asyncio.run(main())
