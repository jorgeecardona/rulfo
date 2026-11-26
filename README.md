# rulfo

Messaging between processes where a request gets zero, one or many replies and always an end.

Status: early. There is no library to install yet. The repository holds the wire format, the
operations, a checked model of the first operation and the frame codec in C.

## What is here

- `docs/operations.md`: what a program can ask rulfo to do.
- `docs/wire-format.md`: the bytes two peers exchange.
- `docs/model.md`: what the model proves, and what it does not.
- `models/request_reply.py`: the model of request and replies.
- `core/`: the frame codec in C11. The API is `core/include/rulfo.h`.

## Run the model

```bash
uv sync
uv run python models/request_reply.py
```

## Test the C core

Needs cmake and a C compiler. The fuzzer needs clang.

```bash
make core-test
```

```bash
make fuzz SECONDS=60
```

## Plan

1. Wire format and operations (done).
2. Model of request and replies (done).
3. Frame codec in C, with a fuzz target (done, in `core/`).
4. Request, reply, end and deadline in the C core.
5. Python binding.

## License

MIT. See [LICENSE](LICENSE).
