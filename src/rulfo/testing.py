"""Helpers for the tests of a program that uses rulfo."""

from __future__ import annotations

import asyncio

from rulfo._transport import Transport


class _MemoryTransport:
    def __init__(self, incoming: asyncio.Queue[bytes], outgoing: asyncio.Queue[bytes]) -> None:
        self._incoming = incoming
        self._outgoing = outgoing
        self._closed = False

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionResetError("the transport is closed")
        self._outgoing.put_nowait(data)

    async def recv(self) -> bytes:
        data = await self._incoming.get()
        if not data:
            self._incoming.put_nowait(b"")  # every later call returns the end too
        return data

    async def close(self) -> None:
        self._closed = True
        self._outgoing.put_nowait(b"")
        self._incoming.put_nowait(b"")


def memory_pair() -> tuple[Transport, Transport]:
    """Two transports connected to each other in memory. Closing one ends both."""
    there: asyncio.Queue[bytes] = asyncio.Queue()
    back: asyncio.Queue[bytes] = asyncio.Queue()
    return _MemoryTransport(back, there), _MemoryTransport(there, back)
