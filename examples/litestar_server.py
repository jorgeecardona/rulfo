"""A Litestar app that serves rulfo on a WebSocket route.

Run: uvicorn --app-dir examples litestar_server:app --port 8000
Needs: pip install litestar uvicorn websockets
"""

import contextlib
from typing import Any

from litestar import Litestar, WebSocket, websocket
from litestar.exceptions import WebSocketDisconnect, WebSocketException

import rulfo

Socket = WebSocket[Any, Any, Any]


class LitestarTransport:
    """Call ``await socket.accept()`` before ``serve``."""

    def __init__(self, socket: Socket) -> None:
        self.socket = socket

    async def send(self, data: bytes) -> None:
        with contextlib.suppress(WebSocketDisconnect):  # recv reports the end
            await self.socket.send_bytes(data)

    async def recv(self) -> bytes:
        try:
            return await self.socket.receive_bytes()
        except WebSocketDisconnect:
            return b""

    async def close(self) -> None:
        with contextlib.suppress(WebSocketDisconnect, WebSocketException):
            await self.socket.close()


async def greet(request: rulfo.Request) -> bytes:
    return f"hello {request.body.decode()}, you are {request.caller}".encode()


server = rulfo.Server(handlers={"greet": greet})


@websocket("/rulfo/{name:str}")
async def endpoint(socket: Socket, name: str) -> None:
    await socket.accept()
    # This example trusts the name in the path. Check here that the caller may use it.
    await server.serve(name, LitestarTransport(socket))


app = Litestar([endpoint], on_shutdown=[server.aclose])
