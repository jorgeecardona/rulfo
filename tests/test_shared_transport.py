"""A transport that shares one connection with other (text) frames.

``close()`` ends the transport without closing the underlying connection. A second
transport can be created on the same connection and ``serve`` called again.

The critical constraint: one reader receives every frame and routes by type. Text
frames must never reach ``recv()`` inside the transport — the websockets library's
``recv(decode=False)`` delivers text as bytes too, so a JSON frame would corrupt
the rulfo stream. ``serve`` runs as a task alongside the reader, not instead of it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from typing import Any

import rulfo


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def limited() -> None:
        async with asyncio.timeout(10):
            await test()

    asyncio.run(limited())


# ---------------------------------------------------------------------------
# Fake websocket: one recv() returns both str and bytes, in order.
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Fake websocket whose recv() yields str and bytes through one queue.

    ``send()`` routes outgoing bytes to ``peer.inbox`` when ``peer`` is set,
    so a connected pair can exchange frames without extra wiring tasks.
    """

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.peer: FakeWebSocket | None = None

    async def recv(self) -> str | bytes:
        return await self.inbox.get()

    async def send(self, data: str | bytes) -> None:
        if self.peer is not None:
            self.peer.inbox.put_nowait(data)

    def push(self, message: str | bytes) -> None:
        """Put a frame directly into this socket's inbox (test helper)."""
        self.inbox.put_nowait(message)


def connected_pair() -> tuple[FakeWebSocket, FakeWebSocket]:
    """Two FakeWebSockets wired so that send on one arrives at the other."""
    a: FakeWebSocket = FakeWebSocket()
    b: FakeWebSocket = FakeWebSocket()
    a.peer = b
    b.peer = a
    return a, b


# ---------------------------------------------------------------------------
# SharedTransport: routes only binary frames from the reader into rulfo.
# ---------------------------------------------------------------------------


class SharedTransport:
    """Transport over binary frames of a shared websocket.

    ``close()`` signals that this transport is done without closing the socket.
    Binary frames arrive through ``feed()``, which the reader loop calls. The
    reader loop also handles text frames; they never reach this transport.
    ``serve`` must run as a task alongside the reader loop.
    """

    def __init__(self, socket: FakeWebSocket) -> None:
        self._socket = socket
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed: asyncio.Event = asyncio.Event()

    def feed(self, data: bytes) -> None:
        """Put one binary frame on the queue. Called by the reader loop."""
        self._queue.put_nowait(data)

    async def send(self, data: bytes) -> None:
        if not self._closed.is_set():
            with contextlib.suppress(Exception):
                await self._socket.send(data)

    async def recv(self) -> bytes:
        get_task: asyncio.Task[bytes] = asyncio.create_task(self._queue.get())
        close_task: asyncio.Task[bool] = asyncio.create_task(self._closed.wait())
        done, pending = await asyncio.wait(
            {get_task, close_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if close_task in done:
            return b""  # transport closed; leave the socket open
        return get_task.result()

    def queue_size(self) -> int:
        """Return the number of binary frames waiting in the queue."""
        return self._queue.qsize()

    async def next_queued(self) -> bytes:
        """Return the next binary frame from the queue."""
        return await self._queue.get()

    @property
    def closed(self) -> bool:
        """True after close() has been called."""
        return self._closed.is_set()

    async def close(self) -> None:
        """End this transport. The socket is not closed."""
        self._closed.set()


async def echo(request: rulfo.Request) -> bytes:
    return request.body


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reader_routes_text_and_binary_through_one_path() -> None:
    """The reader loop branches on type: text to text_list, binary to transport.feed().

    Text frames must never reach the transport queue. This test delivers both
    text and binary frames through the same recv() call, verifying the branching.
    """

    async def test() -> None:
        socket = FakeWebSocket()
        transport = SharedTransport(socket)
        text_received: list[str] = []

        async def reader_loop() -> None:
            # Simulate four frames: text, binary, text, binary.
            for _ in range(4):
                message = await socket.recv()
                if isinstance(message, bytes):
                    transport.feed(message)
                else:
                    text_received.append(message)

        # Push interleaved text and binary frames into one reader.
        socket.push("text-one")
        socket.push(b"binary-one")
        socket.push("text-two")
        socket.push(b"binary-two")

        await reader_loop()

        assert text_received == ["text-one", "text-two"]
        # Binary frames must be in the transport queue, not in text_received.
        assert transport.queue_size() == 2
        assert await transport.next_queued() == b"binary-one"
        assert await transport.next_queued() == b"binary-two"

    run(test)


def test_rulfo_exchange_through_shared_transport() -> None:  # noqa: PLR0915
    """Full rulfo exchange over a shared websocket with interleaved text frames."""

    async def test() -> None:  # noqa: PLR0915
        client_ws, server_ws = connected_pair()

        server_transport = SharedTransport(server_ws)
        client_transport = SharedTransport(client_ws)

        server_peer = rulfo.Peer(handlers={"echo": echo})
        client_peer = rulfo.Peer()

        server_texts: list[str] = []
        client_texts: list[str] = []
        # Signal: reader loops stop after this many frames each.
        server_stop = asyncio.Event()
        client_stop = asyncio.Event()

        async def server_reader() -> None:
            while not server_stop.is_set():
                message = await server_ws.recv()
                if isinstance(message, bytes):
                    server_transport.feed(message)
                else:
                    server_texts.append(message)

        async def client_reader() -> None:
            while not client_stop.is_set():
                message = await client_ws.recv()
                if isinstance(message, bytes):
                    client_transport.feed(message)
                else:
                    client_texts.append(message)

        # Inject text frames before rulfo starts, to prove they don't block it.
        server_ws.push("server-hello")
        client_ws.push("client-hello")

        server_serve = asyncio.create_task(server_peer.serve(server_transport))
        client_serve = asyncio.create_task(client_peer.serve(client_transport))
        server_read = asyncio.create_task(server_reader())
        client_read = asyncio.create_task(client_reader())

        await client_peer.wait_connected()

        # Inject another text frame mid-exchange to prove it does not break rulfo.
        server_ws.push("server-mid")

        result = await client_peer.request("echo", b"hello", timeout=5)
        assert result.outcome is rulfo.Outcome.COMPLETED
        assert result.one() == b"hello"

        # Stop reader loops and transports.
        server_stop.set()
        client_stop.set()
        server_ws.push(b"")  # unblock server_reader
        client_ws.push(b"")  # unblock client_reader

        await server_transport.close()
        await client_transport.close()
        await server_serve
        await client_serve
        server_read.cancel()
        client_read.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_read
        with contextlib.suppress(asyncio.CancelledError):
            await client_read

        # Text frames arrived on the right lists and never reached rulfo.
        assert "server-hello" in server_texts
        assert "server-mid" in server_texts
        assert "client-hello" in client_texts

    run(test)


def test_close_does_not_close_socket() -> None:
    """SharedTransport.close() ends the transport but the socket stays usable."""

    async def test() -> None:
        socket = FakeWebSocket()
        transport = SharedTransport(socket)

        # Text frame pushed before close must still be readable from the socket
        # after the transport closes.
        socket.push("still-alive")

        await transport.close()
        assert transport.closed

        # The socket inbox is unaffected.
        message = await socket.recv()
        assert message == "still-alive"

    run(test)


def test_serve_again_on_new_transport_after_close() -> None:  # noqa: C901, PLR0915
    """After a SharedTransport closes, serve can be called again on a new transport."""

    async def test() -> None:  # noqa: C901, PLR0915
        client_ws, server_ws = connected_pair()

        peer = rulfo.Peer(handlers={"echo": echo})
        client = rulfo.Peer()

        # --- First transport ---
        t1 = SharedTransport(server_ws)
        c1 = SharedTransport(client_ws)

        async def server_reader_1() -> None:
            while True:
                message = await server_ws.recv()
                if isinstance(message, bytes):
                    if message == b"":
                        return
                    t1.feed(message)

        async def client_reader_1() -> None:
            while True:
                message = await client_ws.recv()
                if isinstance(message, bytes):
                    if message == b"":
                        return
                    c1.feed(message)

        s1 = asyncio.create_task(peer.serve(t1))
        sc1 = asyncio.create_task(client.serve(c1))
        sr1 = asyncio.create_task(server_reader_1())
        cr1 = asyncio.create_task(client_reader_1())

        await client.wait_connected()
        r1 = await client.request("echo", b"first", timeout=5)
        assert r1.outcome is rulfo.Outcome.COMPLETED

        # Stop first transport by signalling readers with an empty binary frame.
        server_ws.push(b"")
        client_ws.push(b"")
        await t1.close()
        await c1.close()
        await s1
        await sc1
        await sr1
        await cr1

        # --- Second transport on a new connection ---
        c2_ws, s2_ws = connected_pair()
        t2 = SharedTransport(s2_ws)
        c2 = SharedTransport(c2_ws)

        async def server_reader_2() -> None:
            while True:
                message = await s2_ws.recv()
                if isinstance(message, bytes):
                    if message == b"":
                        return
                    t2.feed(message)

        async def client_reader_2() -> None:
            while True:
                message = await c2_ws.recv()
                if isinstance(message, bytes):
                    if message == b"":
                        return
                    c2.feed(message)

        s2 = asyncio.create_task(peer.serve(t2))
        sc2 = asyncio.create_task(client.serve(c2))
        sr2 = asyncio.create_task(server_reader_2())
        cr2 = asyncio.create_task(client_reader_2())

        await client.wait_connected()
        r2 = await client.request("echo", b"second", timeout=5)
        assert r2.outcome is rulfo.Outcome.COMPLETED

        s2_ws.push(b"")
        c2_ws.push(b"")
        await t2.close()
        await c2.close()
        await s2
        await sc2
        await sr2
        await cr2

    run(test)
