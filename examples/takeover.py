"""A second process finishes a request that the first one sent: request_copy, in one process.

Two ``Peer`` objects stand for two API processes, and a dict stands for the database they share.

Run: python examples/takeover.py
"""

import asyncio
import os
import time
from dataclasses import dataclass

import rulfo
import rulfo.testing


# --8<-- [start:clock]
def wall_clock_ms() -> int:
    """Milliseconds since 1970: a clock that several processes on one machine share."""
    return time.time_ns() // 1_000_000


# --8<-- [end:clock]


@dataclass(frozen=True)
class Stored:
    """A request as the API processes store it before they send it."""

    id: bytes
    key: str
    body: bytes
    deadline_ms: int


runs: list[bytes] = []  # the id of each request that the handler ran


async def resize(request: rulfo.Request) -> bytes:
    runs.append(request.id)
    print(f"[worker] resize runs, repeat={request.repeat}")
    await asyncio.sleep(0.5)
    return b"resized " + request.body


async def main() -> None:
    database: dict[bytes, Stored] = {}
    worker = rulfo.Server(handlers={"resize": resize}, clock=wall_clock_ms)

    # --8<-- [start:first]
    # The first API process stores the request, then sends it.
    first = rulfo.Peer(clock=wall_clock_ms)
    there, back = rulfo.testing.memory_pair()
    connections = [
        asyncio.create_task(worker.serve("api", there)),
        asyncio.create_task(first.serve(back)),
    ]
    await first.wait_connected()

    stored = Stored(os.urandom(16), "resize", b"640x480", wall_clock_ms() + 30_000)
    database[stored.id] = stored
    call = asyncio.create_task(
        first.request_copy(
            stored.id, stored.key, stored.body, deadline_ms=stored.deadline_ms, sent_before=False
        )
    )
    # --8<-- [end:first]

    # The first API process stops before the reply arrives.
    await asyncio.sleep(0.1)
    call.cancel()
    await first.aclose()
    print("[api 1] stopped before the reply")

    # --8<-- [start:second]
    # The second API process finds the request in the database and sends the same values.
    second = rulfo.Peer(clock=wall_clock_ms)
    there, back = rulfo.testing.memory_pair()
    connections += [
        asyncio.create_task(worker.serve("api", there)),  # the same name: the same Peer
        asyncio.create_task(second.serve(back)),
    ]
    for stored in list(database.values()):
        result = await second.request_copy(
            stored.id, stored.key, stored.body, deadline_ms=stored.deadline_ms, sent_before=True
        )
        del database[stored.id]  # before the outcome is reported
        print(f"[api 2] {result.outcome.value} {result.one()!r}")
    # --8<-- [end:second]

    print(f"[worker] the handler ran {len(runs)} time")
    await second.aclose()
    await worker.aclose()
    await asyncio.gather(*connections)


if __name__ == "__main__":
    asyncio.run(main())
