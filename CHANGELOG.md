# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project skeleton.
- Wire format version 1 and the `request` operation, in `docs/`.
- Outcome `busy` for a refused request; `replied` is now `completed`; 16 capability bits.
- Deadlines are times on the requester's clock; a new CLOCK frame lets a replier convert them, so
  a copy that waits in a buffer cannot run twice.
- C frame codec for wire format version 1, with a libFuzzer target, in `core/`.
- Model of request and replies with N repliers, a deadline and faults, in `models/`.
