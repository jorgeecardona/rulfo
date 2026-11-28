# Contributing to rulfo

Developer and maintainer notes: toolchain, the dev loop, CI, and the release process. (Package
*users* need none of this: see the [README](README.md) for what rulfo does and how
to use it.)

## Contributing with an AI agent

Most changes here will be written with a coding agent. To get a good result from yours:

- Point it at [`AGENTS.md`](AGENTS.md) first: the machine-facing contract for this repo. Build
  and test commands, the typed-API and style rules, and the repo-specific gotchas. Agents read it
  automatically when started from the repo root.
- Ship a green `make check` (lint + typecheck + test). Have the agent run it and fix what it
  finds before opening the MR, not leave it for review.
- Don't let it bypass the hooks. The patch version auto-bumps on `src/` changes and CI's
  version-guard enforces it; a `--no-verify` commit fails the pipeline.
- Keep the diff small and typed (pyright runs strict), add a `CHANGELOG.md` entry, and don't
  reformat unrelated files.

## Development & CI

The package holds C code, so the build needs a C compiler (`cc`). The C tests need cmake, and
the fuzz targets need clang.

The toolchain is pinned with [proto](https://moonrepo.dev/proto) (`.prototools`: `moon` + `uv`)
and every CI-graded task is a [moon](https://moonrepo.dev) target running through `uv run`. After
cloning:

```bash
proto install          # installs the pinned moon + uv
uv sync                # install dependencies into .venv
make install-hooks     # pre-commit auto-bump + pre-push checks (one-time)
```

The dev loop (thin `make` aliases over the moon tasks, so `make` runs exactly what CI runs):

```bash
make format            # auto-format + autofix lint (ruff, line length 100)
make check             # the full test stage: lint + typecheck + test + the C tests
make core-test         # the C core with sanitizers
make fuzz SECONDS=60   # libFuzzer, SECONDS for each target
make docs              # build the docs site strictly (as CI / the pages job do)
```

CI runs five GitLab jobs. The main job calls `moon run :ci`, which resolves the full graph
(lint, typecheck, tests, build, wheel test, docs, version-guard). The other four jobs run the
tests on each supported CPython (`test-python`), build and test the macOS wheel (`wheel-macos`),
the Linux aarch64 wheel (`wheel-linux-arm64`), and publish the docs (`pages`). moon owns task
ordering inside `:ci`, so run tasks through `make` or `moon run`, never by hand-wiring them.

## Versioning

The version lives as `__version__` in `src/rulfo/__init__.py`. It is the single source of
truth; `pyproject.toml` reads it via `[tool.hatch.version]` (its `version` is `dynamic`, so there
is no second copy to drift). Do not edit it by hand: the pre-commit hook auto-bumps the patch
version when the shipped source changes (`src/`, `core/src`, `core/include`, `hatch_build.py`),
and CI's `version-guard` enforces that a `src/` change carries a bump (catching `--no-verify`
commits and unhooked clones). `version-ahead` additionally enforces that the version is strictly
greater than `origin/main`. Minor and major bumps require a manual edit of the `__version__` line.

## Releasing

Releases are automated. There is no manual tag or publish step:

1. `pre-commit` auto-bumps the patch version on a change of the shipped source (`make install-hooks`
   once after cloning). Doc/test/example/config-only commits don't bump.
2. `version-guard` (part of `:ci`) enforces the same rule in CI.
3. `:release` runs on a green `main` pipeline (after the full `:ci` graph as deps): if the
   package version isn't on PyPI yet, it publishes via OIDC Trusted Publishing. Merging a
   version bump to `main` releases it. No second pipeline, no tag required.
4. Three jobs gate it. `test-python` runs the tests on CPython 3.12 to 3.15. `wheel-macos`
   and `wheel-linux-arm64` build and test their wheels. A version without both wheels is not
   published: run the pipeline again from GitLab ("Run pipeline"), which runs both jobs.

### One-time setup

PyPI uses OIDC Trusted Publishing (no API token is stored anywhere). PyPI account →
Publishing → add a *pending* GitLab publisher (or add it to the project after the first manual
upload):

| Field | Value |
|-------|-------|
| PyPI Project Name | `rulfo` |
| Namespace | `cardona-oss` |
| Project name (repo) | `rulfo` |
| Top-level pipeline file | `.gitlab-ci.yml` |
| Environment name | `pypi` |

Pushes and MRs run lint + typecheck + test + build + docs only; publishing happens only from a
green `main`.

## License

By contributing, you agree that your contributions are licensed under the MIT License, the same
terms as the project (see [LICENSE](LICENSE)).
