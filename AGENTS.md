# AGENTS.md

Guidance for AI coding agents working in this repo. Humans: read README.md and CHANGELOG.md.

## Overview

rulfo: Messaging between processes where a request gets zero, one or many replies and always an end. Status: design

## Build

- `uv sync` installs dependencies. Toolchain (moon, uv) is pinned in `.prototools`; `proto install` brings it up.
- `make install-hooks` once, to enable the tracked git hooks.

## Test

- `make test` runs the suite.
- `make check` runs lint + typecheck + test (the same stage CI runs via `moon run :ci`).
- Everything in `make check` must pass before you commit.

## Style

- Format and autofix: `make format` (ruff, line length 100).
- Lint: `make lint`. Types: `make typecheck` (pyright strict on `src/`).
- Prefer frozen dataclasses and explicit types. Keep the public API typed (`py.typed`).

## Gotchas

- `models/` holds the musil models. They are the specification: change the model and
  `docs/operations.md` before the code. Each property needs a test where it fails.
- No C code yet. The Python package `rulfo` is empty until the binding exists.

- Version lives as `__version__` in `src/rulfo/__init__.py`; `pyproject.toml` reads it via `[tool.hatch.version]` (`version` is `dynamic`). The pre-commit hook auto-bumps it when `src/` changes. Do not edit versions by hand elsewhere.
- Record follow-ups in `docs/issues/`, not as scattered TODO comments.
