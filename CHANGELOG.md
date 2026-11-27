# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.8] - 2026-11-27

### Changed

- README: overview, outcomes, comparison with HTTP and ZeroMQ, and limits.

## [0.1.7] - 2026-11-27

First release on PyPI.

### Added

- Wire format version 1 and the `request` operation, in `docs/`.
- Outcome `busy` for a refused request; `replied` is now `completed`; 16 capability bits.
- Deadlines are times on the requester's clock; a new CLOCK frame lets a replier convert them, so
  a copy that waits in a buffer cannot run twice.
- A replier can send its replies again when a request comes again, so replies lost in a reconnect
  no longer force `expired`.
- C frame codec for wire format version 1, with a libFuzzer target, in `core/`; the header
  can be read and written without the key and the body.
- Model of request and replies with N repliers, a deadline and faults, in `models/`.
- `rf_peer` in C: handshake, requests, ordered replies, one outcome, resend after a reconnect,
  a store of ids, busy refusal and sending replies again. A test replays model runs on it.
- Python package: `Peer` on asyncio with handlers, outcomes and deadlines, `PeerState` without
  I/O, TCP and unix socket transport, and a memory transport for tests.
- A wheel for CPython 3.12 and later on Linux x86_64.
- A requester can stop waiting for a request (`rf_abandon`, a cancelled `request`).
