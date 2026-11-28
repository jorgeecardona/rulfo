"""One WebSocket for JSON text messages and for rulfo, with the websockets library.

A server and a client in one process. Text messages are the application's own protocol. rulfo
uses the binary messages, after one side sent the text message ``START``.

Run: python examples/shared_websocket.py
Needs: pip install websockets
"""

import asyncio
import contextlib
import json

from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

import rulfo

START = {"type": "rulfo", "v": 1}


# --8<-- [start:transport]
class SharedTransport:
    """The binary messages of a WebSocket that also has text messages.

    The transport never reads the socket: the one reader of the socket calls ``feed`` for each
    binary message. ``close`` ends the transport and leaves the socket open.
    """

    def __init__(self, socket: ClientConnection | ServerConnection) -> None:
        self._socket = socket
        self._messages: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False

    def feed(self, message: bytes) -> None:
        self._messages.put_nowait(message)

    async def send(self, data: bytes) -> None:
        if not self._closed:
            with contextlib.suppress(ConnectionClosed):  # the reader sees the end
                await self._socket.send(data)

    async def recv(self) -> bytes:
        return await self._messages.get()

    async def close(self) -> None:
        self._closed = True
        self._messages.put_nowait(b"")  # recv returns the end


# --8<-- [end:transport]


# --8<-- [start:reader]
async def read_socket(
    socket: ClientConnection | ServerConnection,
    peer: rulfo.Peer,
    label: str,
    transport: SharedTransport | None = None,
) -> None:
    """The one reader of ``socket``: binary messages go to rulfo, text messages are printed.

    The side that sends ``START`` passes its transport. The other side has none until ``START``
    arrives.
    """
    serving: asyncio.Task[None] | None = None
    try:
        async for message in socket:  # str for a text message, bytes for a binary one
            if isinstance(message, bytes):
                if transport is not None:
                    transport.feed(message)
            elif json.loads(message) == START and transport is None:
                transport = SharedTransport(socket)
                serving = asyncio.create_task(peer.serve(transport))
            else:
                print(f"[{label}] text: {message}")
    finally:
        if transport is not None:
            await transport.close()
        if serving is not None:
            await serving


# --8<-- [end:reader]


async def greet(request: rulfo.Request) -> bytes:
    return b"hello " + request.body


async def handle(socket: ServerConnection) -> None:
    """The server side of one connection."""
    await read_socket(socket, rulfo.Peer(handlers={"greet": greet}), "server")


async def main() -> None:
    async with serve(handle, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as socket:
            peer = rulfo.Peer()
            # --8<-- [start:start]
            transport = SharedTransport(socket)
            reading = asyncio.create_task(read_socket(socket, peer, "client", transport))
            await socket.send(json.dumps({"type": "chat", "text": "before rulfo"}))
            await socket.send(json.dumps(START))  # before the first binary message
            serving = asyncio.create_task(peer.serve(transport))
            await peer.wait_connected()
            # --8<-- [end:start]

            result = await peer.request("greet", b"world", timeout=5)
            print("[client] greet:", result.outcome.value, result.one())

            await socket.send(json.dumps({"type": "chat", "text": "between two requests"}))
            result = await peer.request("greet", b"again", timeout=5)
            print("[client] greet:", result.outcome.value, result.one())

            await peer.aclose()
            await serving
            await socket.send(json.dumps({"type": "chat", "text": "after rulfo"}))
        await reading


if __name__ == "__main__":
    asyncio.run(main())
