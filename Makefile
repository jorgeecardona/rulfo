# rulfo -- human task verbs. These are thin aliases: the CI-graded tasks
# (lint/format-check/typecheck/test/docs/build/version-ahead) delegate to the matching
# moon task, so the command + its inputs/outputs live in ONE place (moon.yml) and `make`
# runs exactly what CI runs, with moon's caching. Only the verbs moon has no task for
# (format = write, bump, setup, install-hooks, clean) run a command directly here.
# One-time setup after cloning:  make setup
.DEFAULT_GOAL := help
.PHONY: help setup install-hooks format lint format-check typecheck test core-test binding-asan fuzz check version-ahead bump docs build clean

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## one-time dev setup after cloning: sync deps + install git hooks
	uv sync
	$(MAKE) install-hooks

install-hooks: ## point git at the tracked .githooks/ (pre-commit + pre-push)
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-commit .githooks/pre-push scripts/bump-version scripts/check-version-bump scripts/check-version-ahead scripts/core-test scripts/core-fuzz scripts/build-wheel scripts/test-wheel scripts/binding-asan
	@echo "hooks installed: core.hooksPath -> .githooks"

format: ## auto-format + autofix lint (ruff)
	uv run ruff format src models tests examples hatch_build.py
	uv run ruff check --fix src models tests examples hatch_build.py

lint: ## lint (ruff) -- the CI :lint task
	moon run :lint

format-check: ## fail if anything in the CI scope is not ruff-formatted -- the CI :format-check task
	moon run :format-check

typecheck: ## type-check (pyright) -- the CI :typecheck task
	moon run :typecheck

test: ## run the test suite -- the CI :test task
	moon run :test

core-test: ## build and test the C core with sanitizers -- the CI :core-test task
	moon run :core-test

binding-asan: ## the Python tests on a module with sanitizers -- the CI :binding-asan task
	moon run :binding-asan

fuzz: ## fuzz the C core with libFuzzer (clang) -- SECONDS=60 per target
	./scripts/core-fuzz $(or $(SECONDS),60)

check: lint format-check typecheck test core-test binding-asan ## the CI test stage: lint + format + typecheck + tests

version-ahead: ## fail unless the version is greater than origin/main (when src/ changed) -- the CI :version-ahead task
	moon run :version-ahead

bump: ## bump the version -- TYPE=patch|minor|major (default patch)
	./scripts/bump-version $(or $(TYPE),patch)

docs: ## build docs strictly (as CI / Read the Docs do) -- the CI :docs task
	moon run :docs

build: ## build sdist + wheel into dist/ -- the CI :build task
	moon run :build

clean: ## remove build + test caches
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache site public
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
