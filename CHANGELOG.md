# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.14] - 2026-11-28

### Fixed

- `Server.aclose` closes every connection and cancels every handler before it waits. A handler
  that was slow to stop kept the other clients connected.

### Changed

- API reference: the fields of `Result` and `Reply` are listed.
- Docs say when to use `Peer` and when `Server`, and that the package does not contain the
  example files.

## [0.1.12] - 2026-11-28

### Added

- Quickstart page: a server and a client as two processes, and what the client prints when you
  stop the server during a request.
- Server page: a token check before rulfo starts, TLS, memory for each client, the order to shut
  down, and what `Server` does not have.
- Transports page: the rules for a transport of your own, a Litestar server and a websockets
  client that run together, and one WebSocket for text messages and rulfo.
- Errors page: every exception, when it is raised and what to do.
- Testing page: a pytest file that tests a handler in memory, also across a reconnect.
- `examples/`: 11 programs. The guide shows them, and a test runs each one and compares its
  output with the page.

### Changed

- The guide is now six pages: Quickstart, Peer, Server, Transports, Errors, Testing.
- The reconnect loop of the guide also catches `Overloaded`, and the page says how to end it
  after `aclose`.
- The `request_copy` example is a program that runs, with a clock that two processes share.
- The API reference lists `Peer` and `Server` first, in groups, without the source code.
- `CONTRIBUTING.md` names the five CI jobs.

### Fixed

- The example for a WebSocket with text messages: it ran only with a fake socket. It now runs
  with the websockets library.

## [0.1.11] - 2026-11-28

### Added

- A wheel for Linux aarch64.

## [0.1.10] - 2026-11-27

### Added

- A macOS wheel (macOS 11 or later, Apple silicon and Intel). Sources also build on macOS.
- CPython 3.15 in the tested versions: the tests run on 3.12, 3.13, 3.14 and 3.15.

## [0.1.9] - 2026-11-27

### Added

- `rulfo.Server`: one `Peer` for each client, found by name; `Request.caller` is that name.
- `Peer.request_copy` and `Server.request_copy`: send a request with an id and a deadline you
  chose, so a second process can take it over.

### Changed

- Docs: which messaging needs rulfo covers, plans to cover, and will not build.

## [0.1.8] - 2026-11-27

### Changed

- README: overview, outcomes, comparison with HTTP and ZeroMQ, and limits.

## [0.1.7] - 2026-11-27

First release on PyPI.

### Added

- Wire format version 1 and the `request` operation, documented in `docs/`.
- Outcome `busy` for a refused request; the completed outcome is now named `completed`;
  16 capability bits in the handshake.
- Deadlines are times on the requester's clock; a CLOCK frame lets a replier convert them, so
  a copy that waits in a buffer cannot run twice.
- A replier stores its replies and sends them again when the requester reconnects, so replies
  lost in a reconnect no longer force `expired`.
- C frame codec for wire format version 1, with a libFuzzer target, in `core/`.
- Model of request and replies with a deadline and faults, in `models/`.
- C core: handshake, requests, ordered replies, one outcome per request, resend after a
  reconnect, a store of ids, busy refusal. A conformance test replays model runs against it.
- Python package: `Peer` on asyncio with handlers, outcomes and deadlines, `PeerState` without
  I/O, TCP and unix socket transport, and a memory transport for tests.
- A wheel for CPython 3.12 and later on Linux x86_64.
- Cancelling the task that awaits `request` stops the wait without sending anything.
