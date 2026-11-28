"""The four outcomes, and what the requester gets from each kind of handler, in one process.

Run: python examples/outcomes.py
"""

import asyncio

import rulfo
import rulfo.testing


async def greet(request: rulfo.Request) -> bytes:
    return b"hello " + request.body


async def count(request: rulfo.Request) -> None:
    for number in range(int(request.body)):
        await request.reply(f"item {number}".encode())


async def full(request: rulfo.Request) -> bytes:
    raise rulfo.Busy


async def broken(request: rulfo.Request) -> bytes:
    raise ValueError(f"cannot read {request.body!r}")


async def slow(request: rulfo.Request) -> bytes:
    await asyncio.sleep(60)  # cancelled at the deadline
    return b"too late"


def show(key: str, result: rulfo.Result) -> None:
    replies = [(reply.body, reply.error) for reply in result.replies]
    print(f"{key}: {result.outcome.value} {replies}")


async def main() -> None:
    handlers = {"greet": greet, "count": count, "full": full, "broken": broken, "slow": slow}
    replier = rulfo.Peer(handlers=handlers)
    requester = rulfo.Peer()

    print("Before the connection")
    show("greet", await requester.request("greet", b"world", timeout=5))

    there, back = rulfo.testing.memory_pair()
    connections = [
        asyncio.create_task(replier.serve(there)),
        asyncio.create_task(requester.serve(back)),
    ]
    await requester.wait_connected()

    print("Connected")
    show("greet", await requester.request("greet", b"world", timeout=5))
    show("count", await requester.request("count", b"3", timeout=5))
    show("count", await requester.request("count", b"0", timeout=5))
    show("full", await requester.request("full", timeout=5))
    show("unknown", await requester.request("unknown", timeout=5))
    show("slow", await requester.request("slow", timeout=0.2))

    result = await requester.request("broken", b"x", timeout=5)
    show("broken", result)
    try:
        result.one()
    except rulfo.RemoteError as error:
        print(f"one() raises RemoteError: {error}")

    await requester.aclose()
    await replier.aclose()
    await asyncio.gather(*connections)


if __name__ == "__main__":
    asyncio.run(main())
