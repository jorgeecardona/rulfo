# AGENTS.md

Guidance for AI coding agents working in this repo. Humans: read README.md and CHANGELOG.md.

## Overview

rulfo: messaging between processes where a request gets zero, one or many replies and always an
end. A Python package on a C core.

## Build

- `uv sync` installs dependencies. Toolchain (moon, uv) is pinned in `.prototools`; `proto install` brings it up.
- `make install-hooks` once, to enable the tracked git hooks.

## Test

- `make test` runs the suite.
- `make check` runs lint, format-check, typecheck, the Python tests, the C tests (`core-test`)
  and the Python layer with sanitizers (`binding-asan`).
- `moon run :test-wheel` builds the wheel and installs it on CPython 3.12 to 3.15.
- `sh scripts/test-python` runs the tests on CPython 3.12 to 3.15. CI runs it on Linux (x86_64 and
  aarch64) and on macOS. The sanitizer, core and fuzz tests run on Linux only.
- Everything in `make check` must pass before you commit.

## Style

- Format and autofix: `make format` (ruff, line length 100).
- Lint: `make lint`. Types: `make typecheck` (pyright strict on `src/`).
- Prefer frozen dataclasses and explicit types. Keep the public API typed (`py.typed`).

## Gotchas

- `models/` holds the musil models. They are the specification: change the model and
  `docs/operations.md` before the code. Each property needs a test where it fails.
- `core/` is the C code. `docs/wire-format.md` and `docs/operations.md` are its specification.
  A change to the bytes needs the doc, `core/tests/` and `make fuzz`.
- `hatch_build.py` compiles `core/` into `rulfo._core` (cffi). `uv sync` runs it again after a
  change in `core/include` or `core/src`.
- `rulfo.PeerState` (`_state.py`) is the only Python code that calls the core. `Peer`
  (`_peer.py`) adds asyncio and reads the clock. `tests/test_conformance.py` replays model runs
  on `PeerState`.

- Version lives as `__version__` in `src/rulfo/__init__.py`; `pyproject.toml` reads it via
  `[tool.hatch.version]`. The pre-commit hook bumps the patch version when `src/`, `core/src`,
  `core/include` or `hatch_build.py` change. Do not edit versions by hand elsewhere.
- Record follow-ups in `docs/issues/`, not as scattered TODO comments.
