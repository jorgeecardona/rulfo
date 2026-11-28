"""A server for many workers: it checks a name and a token, then sends each worker a request.

Run: python examples/auth_server.py [port]
With TLS: RULFO_CERT=server.crt RULFO_KEY=server.key python examples/auth_server.py [port]

It stops by itself after worker-1 and worker-2 answered.
"""

import asyncio
import hmac
import os
import ssl
import sys

import rulfo

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7374
TOKENS = {"worker-1": "token-1", "worker-2": "token-2"}  # read yours from a secret store


async def who(request: rulfo.Request) -> bytes:
    """``request.caller`` is the name that ``check_caller`` returned."""
    return f"you are {request.caller}".encode()


# --8<-- [start:config]
# Every worker gets its own Peer with these settings: 256 ids for each one, not in total.
server = rulfo.Server(handlers={"who": who}, config=rulfo.Config(max_ids=256))
# --8<-- [end:config]


# --8<-- [start:check]
async def check_caller(reader: asyncio.StreamReader) -> str | None:
    """The name of the caller, or None for a caller to refuse.

    The first line of a connection is ``name token``. This reads it before rulfo reads anything.
    """
    try:
        async with asyncio.timeout(5):
            line = await reader.readline()
        name, token = line.decode().split()
    except (TimeoutError, OSError, ValueError):
        return None
    expected = TOKENS.get(name)
    if expected is None or not hmac.compare_digest(token.encode(), expected.encode()):
        return None
    return name


async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    name = await check_caller(reader)
    if name is None:
        print("[server] refused a connection", flush=True)
        writer.close()
        return
    print(f"[server] {name} passed the check", flush=True)
    try:
        await server.serve(name, rulfo.StreamTransport(reader, writer))
    except (OSError, rulfo.RulfoError) as error:
        print(f"[server] {name}: {error!r}", flush=True)


# --8<-- [end:check]


# --8<-- [start:tls]
def tls_context() -> ssl.SSLContext | None:
    """TLS when RULFO_CERT and RULFO_KEY name a certificate and its key, or None."""
    if "RULFO_CERT" not in os.environ:
        return None
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(os.environ["RULFO_CERT"], os.environ["RULFO_KEY"])
    return context


# --8<-- [end:tls]


async def main() -> None:
    listener = await asyncio.start_server(accept, "127.0.0.1", PORT, ssl=tls_context())
    print(f"[server] listening on port {PORT}", flush=True)

    # Server has no call that waits for a name: look at ``connected``.
    while server.connected != ("worker-1", "worker-2"):  # noqa: ASYNC110
        await asyncio.sleep(0.1)

    # --8<-- [start:request]
    for name in server.connected:
        result = await server.request(name, "resize", b"640x480", timeout=5)
        print(f"[server] {name}: {result.outcome.value} {result.one()!r}", flush=True)

    result = await server.request("worker-9", "resize", b"640x480", timeout=5)
    print(f"[server] worker-9: {result.outcome.value}", flush=True)
    # --8<-- [end:request]

    # --8<-- [start:shutdown]
    listener.close()  # 1. no new connections
    await server.aclose()  # 2. ends every connection and cancels the handlers
    await listener.wait_closed()  # 3. returns when every call of accept returned
    # --8<-- [end:shutdown]
    print("[server] stopped", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
