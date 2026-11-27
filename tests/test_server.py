"""``Server`` with several parties, and requests that two processes send."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import pytest

from rulfo import CallError, Outcome, Peer, PeerClosed, Request, Result, Server, Transport
from rulfo.testing import memory_pair
from test_peer_asyncio import Gate, echo, run, until


def clock() -> int:
    return time.monotonic_ns() // 1_000_000


@dataclass(frozen=True)
class Connection:
    near: Transport
    served: asyncio.Task[None]  # the call of Server.serve

    async def end(self) -> None:
        """Ends the connection, and waits until the server is done with it."""
        await self.near.close()
        await self.served


def kept(server: Server, name: str) -> bool:
    """Whether the server keeps a ``Peer`` for ``name``."""
    return name in server._parties  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]


class Net:
    """The connections of one test, and the tasks that serve them."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []
        self.peers: list[Peer] = []

    def peer(self, peer: Peer) -> Peer:
        self.peers.append(peer)
        return peer

    async def join(self, server: Server, name: str, client: Peer) -> Connection:
        """Connects ``client`` to ``server`` as ``name``."""
        near, far = memory_pair()
        served = asyncio.create_task(server.serve(name, far))
        self.tasks += [asyncio.create_task(client.serve(near)), served]
        await client.wait_connected()
        await until(lambda: name in server.connected)
        return Connection(near, served)


@contextlib.asynccontextmanager
async def network(server: Server) -> AsyncGenerator[Net]:
    net = Net()
    try:
        yield net
    finally:
        for peer in net.peers:
            await peer.aclose()
        await server.aclose()
        await asyncio.gather(*net.tasks, return_exceptions=True)


def test_a_server_serves_several_parties_by_name() -> None:
    async def who(request: Request) -> bytes:
        return request.caller.encode()

    async def test() -> None:
        server = Server(handlers={"who": who})
        async with network(server) as net:
            for name in ("b", "a"):
                client = net.peer(Peer(handlers={"echo": echo}))
                await net.join(server, name, client)
                assert (await client.request("who", timeout=5)).one() == name.encode()
            assert server.connected == ("a", "b")
            for name in ("a", "b"):
                result = await server.request(name, "echo", name.encode(), timeout=5)
                assert result.one() == name.encode()

    run(test)


def test_a_request_to_an_unknown_name_is_unroutable() -> None:
    async def test() -> None:
        server = Server()
        result = await server.request("nobody", "echo", timeout=5)
        assert result == Result(Outcome.UNROUTABLE, ())
        copy = await server.request_copy(
            "nobody", os.urandom(16), "echo", deadline_ms=0, sent_before=False
        )
        assert copy.outcome is Outcome.EXPIRED
        assert not kept(server, "nobody")
        with pytest.raises(ValueError, match="cannot be sent"):
            await server.request("nobody", "", timeout=5)
        with pytest.raises(ValueError, match="timeout"):
            await server.request("nobody", "echo", timeout=0)
        await server.aclose()
        with pytest.raises(PeerClosed):
            await server.request("nobody", "echo", timeout=5)

    run(test)


def test_a_party_that_reconnects_keeps_its_peer_and_its_request_runs_once() -> None:
    async def test() -> None:
        gate = Gate()
        server = Server(handlers={"gate": gate})
        async with network(server) as net:
            client = net.peer(Peer())
            end = await net.join(server, "a", client)
            call = asyncio.create_task(client.request("gate", b"x", timeout=5))
            await until(lambda: len(gate.started) == 1)
            await end.end()
            await net.join(server, "b", net.peer(Peer()))  # the sweep must keep "a"
            await net.join(server, "a", client)
            gate.open.set()
            result = await call
            assert result.outcome is Outcome.COMPLETED
            assert [r.body for r in result.replies] == [b"first", b"last"]
            assert gate.started == [b"x"]

    run(test)


def test_an_idle_party_without_a_connection_is_dropped_at_the_next_connection() -> None:
    async def test() -> None:
        server = Server()
        async with network(server) as net:
            end = await net.join(server, "a", net.peer(Peer()))
            await end.end()
            assert kept(server, "a")
            await net.join(server, "b", net.peer(Peer()))
            assert not kept(server, "a")

    run(test)


def test_a_closed_server_closes_a_new_connection() -> None:
    async def test() -> None:
        server = Server()
        await server.aclose()
        near, far = memory_pair()
        with pytest.raises(PeerClosed):
            await server.serve("a", far)
        assert await near.recv() == b""

    run(test)


def test_a_second_process_takes_over_a_request_that_runs_once() -> None:
    """The worker keeps one Peer for "the server", whichever process of it connects."""

    async def test() -> None:
        gate = Gate()
        worker = Peer(handlers={"gate": gate}, clock=clock)
        first, second = Server(clock=clock), Server(clock=clock)
        request_id, deadline = os.urandom(16), clock() + 5000
        async with network(first) as one, network(second) as two:
            two.peer(worker)
            await one.join(first, "w", worker)
            lost = asyncio.create_task(
                first.request_copy(
                    "w", request_id, "gate", b"x", deadline_ms=deadline, sent_before=False
                )
            )
            await until(lambda: len(gate.started) == 1)
            await first.aclose()
            with pytest.raises(PeerClosed):
                await lost
            # The second process asks before the worker has connected to it.
            call = asyncio.create_task(
                second.request_copy(
                    "w", request_id, "gate", b"x", deadline_ms=deadline, sent_before=True
                )
            )
            await asyncio.sleep(0.01)
            await two.join(second, "other", two.peer(Peer()))  # the sweep must keep "w"
            assert not call.done()
            await two.join(second, "w", worker)
            gate.open.set()
            result = await call
            assert result.outcome is Outcome.COMPLETED
            assert [r.body for r in result.replies] == [b"first", b"last"]
            assert gate.started == [b"x"]

    run(test)


def test_a_connection_with_the_same_name_replaces_the_first() -> None:
    async def test() -> None:
        server = Server()
        async with network(server) as net:
            first = await net.join(server, "a", net.peer(Peer()))
            await net.join(server, "a", net.peer(Peer(handlers={"echo": echo})))
            await first.served
            await net.join(server, "b", net.peer(Peer()))  # the sweep must keep "a"
            assert (await server.request("a", "echo", b"x", timeout=5)).one() == b"x"

    run(test)


def test_a_copy_that_was_sent_before_to_a_missing_handler_expires() -> None:
    async def test() -> None:
        server = Server(clock=clock)
        async with network(server) as net:
            await net.join(server, "a", net.peer(Peer()))
            result = await server.request_copy(
                "a", os.urandom(16), "none", deadline_ms=clock() + 50, sent_before=True
            )
            assert result.outcome is Outcome.EXPIRED

    run(test)


def test_a_copy_that_was_sent_before_waits_for_a_connection() -> None:
    async def test() -> None:
        server = Server(handlers={"echo": echo})
        client = Peer(clock=clock)
        async with network(server) as net:
            net.peer(client)
            new = await client.request_copy(
                os.urandom(16), "echo", deadline_ms=clock() + 5000, sent_before=False
            )
            assert new.outcome is Outcome.UNROUTABLE
            late = await client.request_copy(
                os.urandom(16), "echo", deadline_ms=clock() + 50, sent_before=True
            )
            assert late.outcome is Outcome.EXPIRED
            call = asyncio.create_task(
                client.request_copy(
                    os.urandom(16), "echo", b"x", deadline_ms=clock() + 5000, sent_before=True
                )
            )
            await asyncio.sleep(0.01)
            assert not call.done()
            assert not client.idle  # a Server must not drop a Peer that a call waits on
            await net.join(server, "a", client)
            assert (await call).one() == b"x"
            assert client.running == 0

    run(test)


def test_request_copy_refuses_what_cannot_be_sent() -> None:
    async def test() -> None:
        server = Server(handlers={"gate": Gate()})
        client = Peer(clock=clock)
        async with network(server) as net:
            net.peer(client)
            soon = clock() + 5000
            with pytest.raises(ValueError, match="16 bytes"):
                await client.request_copy(b"short", "gate", deadline_ms=soon, sent_before=False)
            with pytest.raises(ValueError, match="more than"):
                await client.request_copy(
                    os.urandom(16), "gate", deadline_ms=2**63, sent_before=False
                )
            past = await client.request_copy(
                os.urandom(16), "gate", deadline_ms=0, sent_before=True
            )
            assert past.outcome is Outcome.EXPIRED
            await net.join(server, "a", client)
            request_id = os.urandom(16)
            first = asyncio.create_task(
                client.request_copy(request_id, "gate", deadline_ms=soon, sent_before=False)
            )
            await asyncio.sleep(0.01)
            with pytest.raises(CallError):
                await client.request_copy(request_id, "gate", deadline_ms=soon, sent_before=False)
            assert not first.done()
            first.cancel()

    run(test)
