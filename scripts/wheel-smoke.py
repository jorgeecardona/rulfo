"""Sends one request between two peers of the installed rulfo."""

# ruff: noqa: ASYNC110, INP001, S101, T201 -- a script that scripts/test-wheel runs

import asyncio
import sys

import rulfo
from rulfo.testing import memory_pair


async def echo(request: rulfo.Request) -> bytes:
    return request.body


async def main() -> None:
    client, server = rulfo.Peer(), rulfo.Peer(handlers={"echo": echo})
    ends = memory_pair()
    tasks = [asyncio.create_task(client.serve(ends[0])), asyncio.create_task(server.serve(ends[1]))]
    async with asyncio.timeout(5):
        while not client.connected:
            await asyncio.sleep(0.001)
        assert (await client.request("echo", b"hello", timeout=2)).one() == b"hello"
        await client.aclose()
        await server.aclose()
        await asyncio.gather(*tasks)
    print(f"rulfo {rulfo.__version__} works on Python {sys.version.split()[0]}")


asyncio.run(main())
