"""What carries the bytes of a ``Peer``: any ordered, reliable byte stream."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

_CHUNK = 64 * 1024
_CLOSE_SECONDS = 1.0


class Transport(Protocol):
    """One connection. The cuts between chunks have no meaning."""

    async def send(self, data: bytes) -> None:
        """Sends all of ``data``, at most 64 KiB. Returns when the transport can take more."""
        ...

    async def recv(self) -> bytes:
        """The next bytes that arrived, or ``b""`` when the connection has ended."""
        ...

    async def close(self) -> None:
        """Closes the connection."""
        ...


class StreamTransport:
    """A connection of asyncio streams: TCP and unix sockets."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def recv(self) -> bytes:
        return await self._reader.read(_CHUNK)

    async def close(self) -> None:
        self._writer.close()
        try:
            async with asyncio.timeout(_CLOSE_SECONDS):
                with contextlib.suppress(OSError):  # the connection broke: it is closed
                    await self._writer.wait_closed()
        except TimeoutError:  # the other side reads nothing: drop the waiting bytes
            self._writer.transport.abort()
