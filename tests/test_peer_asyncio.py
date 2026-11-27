"""``Peer`` on asyncio, over transports in memory and one unix socket."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from collections.abc import AsyncGenerator, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from rulfo import (
    Busy,
    Config,
    Handler,
    Outcome,
    Overloaded,
    Peer,
    PeerClosed,
    ProtocolError,
    Reply,
    Request,
    Result,
    StreamTransport,
    Transport,
)
from rulfo.testing import memory_pair


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def limited() -> None:
        async with asyncio.timeout(10):
            await test()

    asyncio.run(limited())


async def until(condition: Callable[[], bool]) -> None:
    while not condition():  # noqa: ASYNC110 -- the tests wait for any condition
        await asyncio.sleep(0.001)


async def echo(request: Request) -> bytes:
    return request.body


class Gate:
    """A handler that sends one reply and then waits to be let through."""

    def __init__(self) -> None:
        self.open = asyncio.Event()
        self.started: list[bytes] = []
        self.cancelled = False

    async def __call__(self, request: Request) -> None:
        self.started.append(request.body)
        await request.reply(b"first")
        try:
            await self.open.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        await request.reply(b"last")


class Wire:
    """A client and a server ``Peer``, and the connection between them."""

    def __init__(
        self,
        handlers: dict[str, Handler],
        client: Config | None = None,
        server: Config | None = None,
    ) -> None:
        self.client = Peer(config=client)
        self.server = Peer(handlers=handlers, config=server)
        self.tasks: list[asyncio.Task[None]] = []
        self.ends: tuple[Transport, Transport] = memory_pair()

    async def connect(self) -> None:
        self.ends = memory_pair()
        self.tasks.append(asyncio.create_task(self.client.serve(self.ends[0])))
        self.tasks.append(asyncio.create_task(self.server.serve(self.ends[1])))
        await self.client.wait_connected()
        await self.server.wait_connected()

    async def disconnect(self) -> None:
        await self.ends[0].close()
        await until(lambda: not self.client.connected and not self.server.connected)


@contextlib.asynccontextmanager
async def wired(
    handlers: dict[str, Handler], client: Config | None = None, server: Config | None = None
) -> AsyncGenerator[Wire]:
    wire = Wire(handlers, client, server)
    await wire.connect()
    try:
        yield wire
    finally:
        await wire.client.aclose()
        await wire.server.aclose()
        await asyncio.gather(*wire.tasks, return_exceptions=True)


def test_the_four_outcomes() -> None:
    async def refuse(request: Request) -> None:
        raise Busy

    async def slow(request: Request) -> None:
        await asyncio.sleep(5)

    async def test() -> None:
        async with wired({"echo": echo, "refuse": refuse, "slow": slow}) as wire:
            done = await wire.client.request("echo", b"hello", timeout=5)
            assert done == Result(Outcome.COMPLETED, (Reply(0, b"hello", error=False),))
            assert done.one() == b"hello"
            assert (await wire.client.request("refuse", timeout=5)).outcome is Outcome.BUSY
            assert (await wire.client.request("none", timeout=5)).outcome is Outcome.UNROUTABLE
            assert (await wire.client.request("slow", timeout=0.05)).outcome is Outcome.EXPIRED

    run(test)


def test_many_replies_arrive_in_order() -> None:
    async def count(request: Request) -> None:
        for n in range(200):
            await request.reply(bytes([n]))

    async def test() -> None:
        async with wired({"count": count}) as wire:
            result = await wire.client.request("count", timeout=5)
            assert result.outcome is Outcome.COMPLETED
            assert [r.body for r in result.replies] == [bytes([n]) for n in range(200)]

    run(test)


def test_both_sides_can_ask() -> None:
    async def test() -> None:
        left, right = Peer(handlers={"echo": echo}), Peer(handlers={"echo": echo})
        ends = memory_pair()
        tasks = [
            asyncio.create_task(left.serve(ends[0])),
            asyncio.create_task(right.serve(ends[1])),
        ]
        await until(lambda: left.connected and right.connected)
        results = await asyncio.gather(
            left.request("echo", b"left", timeout=5), right.request("echo", b"right", timeout=5)
        )
        assert [result.one() for result in results] == [b"left", b"right"]
        await left.aclose()
        await right.aclose()
        await asyncio.gather(*tasks)

    run(test)


def test_a_handler_that_raises_sends_an_error_reply() -> None:
    async def broken(request: Request) -> bytes:
        raise KeyError("missing")

    async def late_refusal(request: Request) -> None:
        await request.reply(b"one")
        raise Busy

    async def inner_timeout(request: Request) -> None:
        async with asyncio.timeout(0):
            await asyncio.sleep(1)

    async def test() -> None:
        handlers: dict[str, Handler] = {"a": broken, "b": late_refusal, "c": inner_timeout}
        async with wired(handlers) as wire:
            for key, start in (("a", b"KeyError"), ("b", b"Busy"), ("c", b"TimeoutError")):
                result = await wire.client.request(key, timeout=5)
                assert result.outcome is Outcome.COMPLETED
                assert result.replies[-1].error
                assert result.replies[-1].body.startswith(start)

    run(test)


def test_waiting_for_a_connection_ends_when_the_peer_closes() -> None:
    async def test() -> None:
        peer = Peer()
        waiting = asyncio.create_task(peer.wait_connected())
        await asyncio.sleep(0.01)
        assert not waiting.done()
        await peer.aclose()
        with pytest.raises(PeerClosed):
            await waiting

    run(test)


def test_without_a_connection_the_outcome_is_unroutable_at_once() -> None:
    async def test() -> None:
        peer = Peer()
        assert (await peer.request("echo", timeout=5)).outcome is Outcome.UNROUTABLE
        assert peer.idle
        await peer.aclose()

    run(test)


def test_a_request_waits_for_the_handshake() -> None:
    async def test() -> None:
        client, server = Peer(), Peer(handlers={"echo": echo})
        ends = memory_pair()
        tasks = [asyncio.create_task(client.serve(ends[0]))]
        await asyncio.sleep(0)
        asking = asyncio.create_task(client.request("echo", b"x", timeout=5))
        await asyncio.sleep(0.01)
        assert not asking.done()
        tasks.append(asyncio.create_task(server.serve(ends[1])))
        assert (await asking).one() == b"x"
        await client.aclose()
        await server.aclose()
        await asyncio.gather(*tasks)

    run(test)


def test_a_connection_that_ends_during_the_handshake_is_unroutable() -> None:
    async def test() -> None:
        client = Peer()
        ends = memory_pair()
        serving = asyncio.create_task(client.serve(ends[0]))
        await asyncio.sleep(0)
        asking = asyncio.create_task(client.request("echo", timeout=5))
        await asyncio.sleep(0.01)
        await ends[1].close()
        assert (await asking).outcome is Outcome.UNROUTABLE
        await serving
        await client.aclose()

    run(test)


def test_a_request_survives_a_reconnect_and_runs_once() -> None:
    async def test() -> None:
        gate = Gate()
        async with wired({"gate": gate}) as wire:
            asking = asyncio.create_task(wire.client.request("gate", b"x", timeout=5))
            await until(lambda: gate.started == [b"x"])
            await wire.disconnect()
            gate.open.set()  # the last reply and the end are made without a connection
            await until(lambda: not wire.server.running)
            await wire.connect()
            result = await asking
            assert result.outcome is Outcome.COMPLETED
            assert [r.body for r in result.replies] == [b"first", b"last"]
            assert gate.started == [b"x"]

    run(test)


def test_a_second_serve_replaces_the_first() -> None:
    async def test() -> None:
        async with wired({"echo": echo}) as wire:
            first = list(wire.tasks)
            await wire.connect()
            await asyncio.gather(*first)
            assert (await wire.client.request("echo", b"x", timeout=5)).one() == b"x"

    run(test)


def test_cancelling_a_request_frees_its_slot_and_tells_nobody() -> None:
    async def test() -> None:
        gate = Gate()
        async with wired({"gate": gate, "echo": echo}, client=Config(max_requests=1)) as wire:
            asking = asyncio.create_task(wire.client.request("gate", timeout=5))
            await until(lambda: len(gate.started) == 1)
            asking.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asking
            assert (await wire.client.request("echo", b"x", timeout=5)).one() == b"x"
            assert not gate.cancelled
            gate.open.set()
            await until(lambda: not wire.server.running)

    run(test)


def test_a_request_waits_for_a_free_slot() -> None:
    async def test() -> None:
        gate = Gate()
        async with wired({"gate": gate, "echo": echo}, client=Config(max_requests=1)) as wire:
            first = asyncio.create_task(wire.client.request("gate", timeout=5))
            await until(lambda: len(gate.started) == 1)
            short = await wire.client.request("echo", timeout=0.05)
            assert short.outcome is Outcome.EXPIRED
            second = asyncio.create_task(wire.client.request("echo", b"x", timeout=5))
            await asyncio.sleep(0.01)
            assert not second.done()
            gate.open.set()
            assert (await first).outcome is Outcome.COMPLETED
            assert (await second).one() == b"x"

    run(test)


def test_a_handler_is_cancelled_at_the_deadline() -> None:
    async def test() -> None:
        gate = Gate()
        async with wired({"gate": gate}) as wire:
            result = await wire.client.request("gate", timeout=0.05)
            assert result.outcome is Outcome.EXPIRED
            assert [r.body for r in result.replies] == [b"first"]
            await until(lambda: gate.cancelled and not wire.server.running)

    run(test)


def test_a_handler_that_swallows_the_cancellation_does_not_complete_the_request() -> None:
    async def stubborn(request: Request) -> bytes:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(5)
        return b"partial"

    async def test() -> None:
        async with wired({"stubborn": stubborn}) as wire:
            result = await wire.client.request("stubborn", timeout=0.05)
            assert result.outcome is Outcome.EXPIRED
            await until(lambda: not wire.server.running)

    run(test)


def test_requests_above_max_running_are_busy() -> None:
    async def test() -> None:
        gate = Gate()
        async with wired({"gate": gate}, server=Config(max_running=1)) as wire:
            first = asyncio.create_task(wire.client.request("gate", timeout=5))
            await until(lambda: len(gate.started) == 1)
            assert (await wire.client.request("gate", timeout=5)).outcome is Outcome.BUSY
            gate.open.set()
            assert (await first).outcome is Outcome.COMPLETED

    run(test)


def test_closing_fails_the_requests_and_cancels_the_handlers() -> None:
    async def test() -> None:
        gate = Gate()
        async with wired({"gate": gate}) as wire:
            asking = asyncio.create_task(wire.client.request("gate", timeout=5))
            await until(lambda: len(gate.started) == 1)
            await wire.server.aclose()
            assert gate.cancelled
            await asyncio.sleep(0.01)
            assert not asking.done()  # no end was sent: the requester waits for the deadline
            await wire.client.aclose()
            with pytest.raises(PeerClosed):
                await asking
            with pytest.raises(PeerClosed):
                await wire.client.request("gate", timeout=5)
            with pytest.raises(PeerClosed):
                await wire.client.serve(memory_pair()[0])

    run(test)


class Stuck:
    """A transport that sends only while ``open`` is set."""

    def __init__(self, inner: Transport) -> None:
        self.inner = inner
        self.open = asyncio.Event()
        self.open.set()

    async def send(self, data: bytes) -> None:
        await self.open.wait()
        await self.inner.send(data)

    async def recv(self) -> bytes:
        return await self.inner.recv()

    async def close(self) -> None:
        await self.inner.close()


def test_reply_waits_while_the_connection_is_slow() -> None:
    sent: list[int] = []

    async def flood(request: Request) -> None:
        for n in range(10):
            await request.reply(bytes(1000))
            sent.append(n)

    async def test() -> None:
        client = Peer()
        server = Peer(handlers={"flood": flood}, config=Config(send_buffer=2500))
        ends = memory_pair()
        slow = Stuck(ends[1])
        tasks = [
            asyncio.create_task(client.serve(ends[0])),
            asyncio.create_task(server.serve(slow)),
        ]
        await until(lambda: client.connected and server.connected)
        slow.open.clear()
        asking = asyncio.create_task(client.request("flood", timeout=5))
        await asyncio.sleep(0.05)
        assert 0 < len(sent) < 5
        slow.open.set()
        assert len((await asking).replies) == 10
        await client.aclose()
        await server.aclose()
        await asyncio.gather(*tasks)

    run(test)


def test_the_transport_gets_pieces_of_at_most_64_kib() -> None:
    sizes: list[int] = []

    class Counting(Stuck):
        async def send(self, data: bytes) -> None:
            sizes.append(len(data))
            await super().send(data)

    async def test() -> None:
        client, server = Peer(), Peer(handlers={"echo": echo})
        ends = memory_pair()
        tasks = [
            asyncio.create_task(client.serve(Counting(ends[0]))),
            asyncio.create_task(server.serve(Counting(ends[1]))),
        ]
        await client.wait_connected()
        body = bytes(1_000_000)
        assert (await client.request("echo", body, timeout=5)).one() == body
        assert max(sizes) == 64 * 1024
        await client.aclose()
        await server.aclose()
        await asyncio.gather(*tasks)

    run(test)


def test_bytes_of_another_protocol_raise() -> None:
    async def test() -> None:
        peer = Peer()
        ends = memory_pair()
        await ends[1].send(b"HTTP/1.1 200 OK\r\n")
        with pytest.raises(ProtocolError, match="RF_ERR_MAGIC"):
            await peer.serve(ends[0])
        assert not peer.connected
        await peer.aclose()

    run(test)


def test_a_handshake_that_takes_too_long_raises() -> None:
    async def test() -> None:
        peer = Peer(config=Config(handshake=0.05))
        with pytest.raises(ProtocolError, match="RF_ERR_HANDSHAKE"):
            await peer.serve(memory_pair()[0])
        await peer.aclose()

    run(test)


def test_an_error_of_the_transport_leaves_serve() -> None:
    class Broken(Stuck):
        async def recv(self) -> bytes:
            raise ConnectionResetError

    async def test() -> None:
        peer = Peer()
        with pytest.raises(ConnectionResetError):
            await peer.serve(Broken(memory_pair()[0]))
        await peer.aclose()

    run(test)


class SlowToCancel(Stuck):
    """A transport whose ``recv`` needs a moment to stop."""

    async def recv(self) -> bytes:
        try:
            return await self.inner.recv()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise


def test_cancelling_serve_twice_leaves_the_peer_usable() -> None:
    async def test() -> None:
        peer = Peer()
        first = asyncio.create_task(peer.serve(SlowToCancel(memory_pair()[0])))
        await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.sleep(0.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        ends = memory_pair()
        server = Peer(handlers={"echo": echo})
        tasks = [
            asyncio.create_task(peer.serve(ends[0])),
            asyncio.create_task(server.serve(ends[1])),
        ]
        await peer.wait_connected()
        assert (await peer.request("echo", b"x", timeout=5)).one() == b"x"
        await peer.aclose()
        await server.aclose()
        await asyncio.gather(*tasks)

    run(test)


def test_serve_cancelled_while_it_waits_closes_its_transport() -> None:
    async def test() -> None:
        peer = Peer()
        first = asyncio.create_task(peer.serve(SlowToCancel(memory_pair()[0])))
        await asyncio.sleep(0.01)
        ends = memory_pair()
        second = asyncio.create_task(peer.serve(ends[0]))
        await asyncio.sleep(0)  # it waits for the first to stop
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert await ends[1].recv() == b""
        await peer.aclose()
        await first

    run(test)


def test_a_cancelled_close_still_fails_the_requests_and_cancels_the_handlers() -> None:
    async def test() -> None:
        gate = Gate()
        client, server = Peer(), Peer(handlers={"gate": gate})
        ends = memory_pair()
        tasks = [
            asyncio.create_task(client.serve(SlowToCancel(ends[0]))),
            asyncio.create_task(server.serve(SlowToCancel(ends[1]))),
        ]
        await client.wait_connected()
        asking = asyncio.create_task(client.request("gate", timeout=5))
        await until(lambda: len(gate.started) == 1)
        for peer in (client, server):
            closing = asyncio.create_task(peer.aclose())
            await asyncio.sleep(0)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
        with pytest.raises(PeerClosed):
            await asking
        await until(lambda: gate.cancelled)
        await asyncio.gather(*tasks)

    run(test)


def test_a_side_that_stops_reading_ends_the_connection() -> None:
    async def test() -> None:
        client = Peer()
        server = Peer(config=Config(send_buffer=100, replay_bytes=0, max_length=100))
        ends = memory_pair()
        slow = Stuck(ends[1])
        serving = asyncio.create_task(server.serve(slow))
        asking_side = asyncio.create_task(client.serve(ends[0]))
        await client.wait_connected()
        await server.wait_connected()
        slow.open.clear()  # the server's refusals pile up
        requests = [asyncio.create_task(client.request("none", timeout=5)) for _ in range(40)]
        with pytest.raises(Overloaded):
            await serving
        await client.aclose()
        await server.aclose()
        await asyncio.gather(*requests, return_exceptions=True)
        await asking_side

    run(test)


def test_large_open_requests_are_sent_again_without_overload() -> None:
    async def test() -> None:
        gate = Gate()
        config = Config(send_buffer=100, replay_bytes=0, max_length=2000)
        async with wired({"gate": gate}, client=config) as wire:
            requests = [
                asyncio.create_task(wire.client.request("gate", bytes(1900), timeout=5))
                for _ in range(20)
            ]
            await until(lambda: len(gate.started) == 20)
            await wire.disconnect()
            await wire.connect()
            await asyncio.sleep(0.05)
            assert wire.client.connected
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            gate.open.set()

    run(test)


def test_a_handler_can_close_its_peer() -> None:
    async def test() -> None:
        async def stop(request: Request) -> None:
            await wire.server.aclose()

        wire = Wire({"stop": stop})
        await wire.connect()
        asking = asyncio.create_task(wire.client.request("stop", timeout=0.2))
        assert (await asking).outcome is Outcome.EXPIRED
        await until(lambda: not wire.server.running)
        await wire.client.aclose()
        await asyncio.gather(*wire.tasks, return_exceptions=True)

    run(test)


def test_an_error_while_sending_leaves_serve() -> None:
    class Broken(Stuck):
        async def send(self, data: bytes) -> None:
            raise ConnectionResetError

    async def test() -> None:
        peer = Peer()
        with pytest.raises(ConnectionResetError):
            await peer.serve(Broken(memory_pair()[0]))
        await peer.aclose()

    run(test)


def test_a_handler_that_returns_something_else_sends_an_error() -> None:
    async def number(request: Request) -> bytes:
        return 5  # pyright: ignore[reportReturnType]

    async def test() -> None:
        async with wired({"number": number}) as wire:
            result = await wire.client.request("number", timeout=5)
            assert [r.error for r in result.replies] == [True]
            assert result.replies[0].body.startswith(b"TypeError")

    run(test)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 1e12])
def test_a_timeout_out_of_range(timeout: float) -> None:
    async def test() -> None:
        with pytest.raises(ValueError, match="timeout"):
            await Peer().request("k", timeout=timeout)

    run(test)


@pytest.mark.parametrize(("key", "size"), [("", 0), ("k", 100), ("k" * 70000, 0)])
def test_a_request_that_cannot_be_sent(key: str, size: int) -> None:
    async def test() -> None:
        peer = Peer(config=Config(max_length=100))
        with pytest.raises(ValueError, match="cannot be sent"):
            await peer.request(key, bytes(size), timeout=5)

    run(test)


def test_over_a_unix_socket() -> None:
    # Not tmp_path: macOS refuses a socket path above 104 bytes.
    with tempfile.TemporaryDirectory() as folder:
        _over_a_unix_socket(Path(folder) / "s")


def _over_a_unix_socket(path: Path) -> None:
    async def test() -> None:
        server = Peer(handlers={"echo": echo})

        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await server.serve(StreamTransport(reader, writer))

        listening = await asyncio.start_unix_server(accept, path)
        client = Peer()
        reader, writer = await asyncio.open_unix_connection(path)
        serving = asyncio.create_task(client.serve(StreamTransport(reader, writer)))
        await until(lambda: client.connected)
        body = bytes(range(256)) * 4096  # 1 MiB
        assert (await client.request("echo", body, timeout=5)).one() == body
        await client.aclose()
        await serving
        await server.aclose()
        listening.close()
        await listening.wait_closed()

    run(test)
