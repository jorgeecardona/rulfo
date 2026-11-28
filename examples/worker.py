"""A worker for examples/auth_server.py: answers resize, and asks the server who it is.

Run: python examples/worker.py name token [port]
With TLS: RULFO_CA=server.crt python examples/worker.py name token [port]

It exits when the server closes the connection.
"""

import asyncio
import os
import ssl
import sys

import rulfo

NAME, TOKEN = sys.argv[1], sys.argv[2]
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 7374


async def resize(request: rulfo.Request) -> bytes:
    return f"{NAME} resized to ".encode() + request.body


def tls_context() -> ssl.SSLContext | None:
    """TLS that trusts the certificate file in RULFO_CA, or None."""
    if "RULFO_CA" not in os.environ:
        return None
    return ssl.create_default_context(cafile=os.environ["RULFO_CA"])


async def main() -> None:
    peer = rulfo.Peer(handlers={"resize": resize})
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT, ssl=tls_context())
    writer.write(f"{NAME} {TOKEN}\n".encode())  # before rulfo sends anything
    connection = asyncio.create_task(peer.serve(rulfo.StreamTransport(reader, writer)))

    # A server that refuses the token closes the connection: then serve returns first.
    ready = asyncio.create_task(peer.wait_connected())
    await asyncio.wait({connection, ready}, return_when=asyncio.FIRST_COMPLETED)
    if not ready.done():
        ready.cancel()
        print(f"[{NAME}] the server refused the connection", flush=True)
        return

    result = await peer.request("who", timeout=5)
    print(f"[{NAME}] who: {result.one()!r}", flush=True)

    await connection
    print(f"[{NAME}] the server closed the connection", flush=True)
    await peer.aclose()


if __name__ == "__main__":
    asyncio.run(main())
