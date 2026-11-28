"""A client over a WebSocket, with the websockets library. It sends one request and exits.

Run: python examples/websocket_client.py [url]
Needs: pip install websockets
"""

import asyncio
import contextlib
import sys

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

import rulfo

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8000/rulfo/client-1"


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


async def keep_connected(peer: rulfo.Peer) -> None:
    """``connect`` as an iterator connects again when a connection ends, with a growing wait."""
    async for socket in connect(URL):
        with contextlib.suppress(rulfo.ProtocolError, rulfo.Overloaded):
            await peer.serve(WebsocketsTransport(socket))


async def main() -> None:
    peer = rulfo.Peer()
    connection = asyncio.create_task(keep_connected(peer))
    await peer.wait_connected()

    result = await peer.request("greet", b"world", timeout=5)
    print("greet:", result.outcome.value, result.one())

    await peer.aclose()
    connection.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await connection


if __name__ == "__main__":
    asyncio.run(main())
