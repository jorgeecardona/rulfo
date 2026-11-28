# Testing

Test your handlers and your calls with two `Peer` objects in one process, connected in memory.
No socket, no port, and no pytest plugin for asyncio.

## A test file that runs

`rulfo.testing.memory_pair()` returns two transports that are connected to each other. Give
one to each `Peer`. Closing one ends both.

`examples/test_add.py` tests a handler named `add`. Copy `connected` into your tests:

```python
--8<-- "examples/test_add.py"
```

Run it:

```bash
pip install rulfo pytest
pytest examples/test_add.py
```

```text
examples/test_add.py ...                                                 [100%]

============================== 3 passed in 0.22s ===============================
```

## What the three tests show

- A reply: bodies are bytes, so the test encodes and decodes the JSON itself. rulfo has no
  serialization.
- An error reply: a handler that raises gives `completed` with an error reply, and `one()`
  raises `RemoteError`. rulfo also logs the traceback on the `rulfo` logger.
- A reconnect: a second `memory_pair()` given to `serve` replaces the first connection. The
  request stays open, and the handler runs once.

## Rules for your tests

- Use the same `Peer` objects that your program uses, with a memory transport in place of the
  socket. A test that goes around `Peer` does not test the deadlines or the outcomes.
- Limit the time of each test. `connected` uses `asyncio.timeout(5)`, so a request that never
  ends fails the test.
- Close both peers at the end, then wait for the calls of `serve`. The test then also fails on
  an error that `serve` raised.
- Deadlines use real time. To test `expired`, pass a short `timeout`, such as 0.2 seconds, as
  `examples/outcomes.py` in [Peer](python.md#answer-requests) does.
- A `Server` works the same way: `await server.serve("name", transport)`.

`rulfo.testing` is part of the installed package. The [API reference](api.md) lists it.
