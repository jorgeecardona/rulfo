# AGENTS.md

Guidance for AI coding agents working in this repo. Humans: read README.md and CHANGELOG.md.

## Overview

rulfo: Messaging between processes where a request gets zero, one or many replies and always an end. Status: design

## Build

- `uv sync` installs dependencies. Toolchain (moon, uv) is pinned in `.prototools`; `proto install` brings it up.
- `make install-hooks` once, to enable the tracked git hooks.

## Test

- `make test` runs the suite.
- `make check` runs lint, format-check, typecheck, the Python tests and the C tests (`core-test`).
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
- `rulfo.PeerState` is the only Python code that calls the core. `tests/test_conformance.py`
  replays model runs on it.

- Version lives as `__version__` in `src/rulfo/__init__.py`; `pyproject.toml` reads it via `[tool.hatch.version]` (`version` is `dynamic`). The pre-commit hook auto-bumps it when `src/` changes. Do not edit versions by hand elsewhere.
- Record follow-ups in `docs/issues/`, not as scattered TODO comments.
