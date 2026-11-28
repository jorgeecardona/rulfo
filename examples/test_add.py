"""Tests of a handler, without a socket.

Run: pytest examples/test_add.py
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator

import pytest

import rulfo
import rulfo.testing


async def add(request: rulfo.Request) -> bytes:
    """The handler under test: the body is a JSON list of numbers, the reply is their sum."""
    numbers = json.loads(request.body)
    return json.dumps(sum(numbers)).encode()


@contextlib.asynccontextmanager
async def connected(replier: rulfo.Peer, requester: rulfo.Peer) -> AsyncGenerator[None]:
    """Connect two peers in memory. Close both at the end."""
    there, back = rulfo.testing.memory_pair()
    connections = [
        asyncio.create_task(replier.serve(there)),
        asyncio.create_task(requester.serve(back)),
    ]
    try:
        async with asyncio.timeout(5):  # a test that hangs fails after 5 seconds
            await requester.wait_connected()
            yield
    finally:
        await requester.aclose()
        await replier.aclose()
        await asyncio.gather(*connections)


def test_add_replies_with_the_sum() -> None:
    async def scenario() -> None:
        requester = rulfo.Peer()
        async with connected(rulfo.Peer(handlers={"add": add}), requester):
            result = await requester.request("add", json.dumps([3, 4]).encode(), timeout=5)
            assert result.outcome is rulfo.Outcome.COMPLETED
            assert json.loads(result.one()) == 7

    asyncio.run(scenario())


def test_a_body_that_is_not_json_gives_an_error_reply() -> None:
    async def scenario() -> None:
        requester = rulfo.Peer()
        async with connected(rulfo.Peer(handlers={"add": add}), requester):
            result = await requester.request("add", b"not json", timeout=5)
            assert result.outcome is rulfo.Outcome.COMPLETED
            assert result.replies[0].error
            with pytest.raises(rulfo.RemoteError, match="JSONDecodeError"):
                result.one()

    asyncio.run(scenario())


def test_a_request_survives_a_reconnect_and_runs_once() -> None:
    async def scenario() -> None:
        runs = 0
        started = asyncio.Event()

        async def slow_add(request: rulfo.Request) -> bytes:
            nonlocal runs
            runs += 1
            started.set()
            await asyncio.sleep(0.1)
            return await add(request)

        replier = rulfo.Peer(handlers={"add": slow_add})
        requester = rulfo.Peer()
        async with connected(replier, requester):
            call = asyncio.create_task(requester.request("add", b"[1, 2]", timeout=5))
            await started.wait()

            # Drop the connection while the handler runs, then connect the same peers again.
            there, back = rulfo.testing.memory_pair()
            again = [
                asyncio.create_task(replier.serve(there)),
                asyncio.create_task(requester.serve(back)),
            ]
            assert json.loads((await call).one()) == 3
            assert runs == 1
            await requester.aclose()
            await replier.aclose()
            await asyncio.gather(*again)

    asyncio.run(scenario())
