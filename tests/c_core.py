"""The C core called through ``ctypes``, for the tests. Not the Python binding of rulfo."""

from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

CORE = Path(__file__).resolve().parent.parent / "core"
HEADER_SIZE = 32


def _constants(prefix: str) -> dict[str, int]:
    """The values of the enum in ``rulfo.h`` whose first name is ``prefix``, read from it."""
    text = re.sub(r"/\*.*?\*/", "", (CORE / "include" / "rulfo.h").read_text(), flags=re.DOTALL)
    for body in re.findall(r"typedef enum \{(.*?)\}", text, flags=re.DOTALL):
        entries = [entry.split("=") for entry in body.split(",")]
        if entries[0][0].strip() != prefix:
            continue
        values: dict[str, int] = {}
        number = -1
        for entry in entries:
            number = int(entry[1], 0) if len(entry) > 1 else number + 1
            values[entry[0].strip()] = number
        return values
    msg = f"rulfo.h has no enum that starts with {prefix}"
    raise LookupError(msg)


STATUS = _constants("RF_OK")
EVENT = _constants("RF_EV_NONE")
OUTCOME = _constants("RF_COMPLETED")
END_BUSY = 0x02


class _Config(ctypes.Structure):
    _fields_: ClassVar = [
        (name, ctypes.c_uint32)
        for name in (
            "max_length",
            "max_out",
            "handshake_ms",
            "keep_ms",
            "max_requests",
            "max_ids",
            "max_deadline_ms",
        )
    ]


class _Id(ctypes.Structure):
    _fields_: ClassVar = [("bytes", ctypes.c_uint8 * 16)]


class _Bytes(ctypes.Structure):
    _fields_: ClassVar = [("ptr", ctypes.c_void_p), ("len", ctypes.c_size_t)]


class _Event(ctypes.Structure):
    _fields_: ClassVar = [
        ("kind", ctypes.c_uint8),
        ("flags", ctypes.c_uint8),
        ("outcome", ctypes.c_uint8),
        ("ended", ctypes.c_uint8),
        ("status", ctypes.c_int),
        ("id", _Id),
        ("number", ctypes.c_uint64),
        ("deadline", ctypes.c_uint64),
        ("key", _Bytes),
        ("body", _Bytes),
    ]


@dataclass(frozen=True)
class Event:
    """An ``rf_event`` with its key and body copied."""

    kind: int
    outcome: int
    ended: bool
    number: int
    deadline: int
    body: bytes


def build(directory: Path) -> ctypes.CDLL:
    """Compile the core into a shared library in ``directory`` and load it."""
    compiler = shutil.which("cc")
    if compiler is None:
        msg = "the conformance test needs a C compiler (cc)"
        raise RuntimeError(msg)
    library = directory / "librulfo.so"
    sources = sorted(str(path) for path in (CORE / "src").glob("*.c"))
    command = [compiler, "-std=c11", "-shared", "-fPIC", "-O1", "-Werror", "-Wall"]
    command += [f"-I{CORE / 'include'}", *sources, "-o", str(library)]
    subprocess.run(command, check=True)  # noqa: S603
    loaded = ctypes.CDLL(str(library))
    loaded.rf_peer_size.restype = ctypes.c_size_t
    loaded.rf_output.restype = ctypes.c_size_t
    return loaded


def _copy(data: _Bytes) -> bytes:
    return ctypes.string_at(data.ptr, data.len) if data.len else b""


def _event(event: _Event) -> Event:
    return Event(
        kind=event.kind,
        outcome=event.outcome,
        ended=bool(event.ended),
        number=event.number,
        deadline=event.deadline,
        body=_copy(event.body),
    )


class Peer:
    """One ``rf_peer``. Each call that writes a header returns the status and the header."""

    def __init__(self, library: ctypes.CDLL, keep_ms: int) -> None:
        self._c = library
        config = _Config()
        library.rf_config_default(ctypes.byref(config))
        config.keep_ms = keep_ms
        size = library.rf_peer_size(ctypes.byref(config))
        self._memory = ctypes.create_string_buffer(size)
        self._peer = ctypes.c_void_p()
        status = library.rf_peer_init(
            self._memory,
            ctypes.c_size_t(size),
            ctypes.byref(config),
            ctypes.byref(self._peer),
        )
        if status != STATUS["RF_OK"]:
            msg = f"rf_peer_init returned {status}"
            raise RuntimeError(msg)

    def connected(self, now: int) -> None:
        self._c.rf_connected(self._peer, ctypes.c_uint64(now))

    def disconnected(self) -> None:
        self._c.rf_disconnected(self._peer)

    def output(self) -> bytes:
        buffer = ctypes.create_string_buffer(4096)
        count = self._c.rf_output(self._peer, buffer, ctypes.c_size_t(len(buffer)))
        return buffer.raw[:count]

    def input(self, data: bytes, now: int) -> tuple[int, int, Event]:
        """One ``rf_input``: its status, the bytes used and the event."""
        buffer = ctypes.create_string_buffer(data, len(data))
        used = ctypes.c_size_t()
        event = _Event()
        status = self._c.rf_input(
            self._peer,
            buffer,
            ctypes.c_size_t(len(data)),
            ctypes.c_uint64(now),
            ctypes.byref(used),
            ctypes.byref(event),
        )
        return status, used.value, _event(event)

    def poll(self, now: int) -> Event:
        event = _Event()
        self._c.rf_poll(self._peer, ctypes.c_uint64(now), ctypes.byref(event))
        return _event(event)

    def request(
        self, request_id: bytes, key: bytes, deadline: int, now: int, *, repeat: bool
    ) -> tuple[int, bytes]:
        key_buffer = ctypes.create_string_buffer(key, len(key))
        header = ctypes.create_string_buffer(HEADER_SIZE)
        status = self._c.rf_request(
            self._peer,
            _id(request_id),
            _Bytes(ctypes.cast(key_buffer, ctypes.c_void_p), len(key)),
            ctypes.c_size_t(0),
            ctypes.c_uint64(deadline),
            ctypes.c_int(repeat),
            ctypes.c_uint64(now),
            header,
        )
        return status, header.raw + key

    def reply(self, request_id: bytes, body: bytes) -> tuple[int, bytes]:
        header = ctypes.create_string_buffer(HEADER_SIZE)
        status = self._c.rf_reply(
            self._peer, _id(request_id), ctypes.c_size_t(len(body)), ctypes.c_uint8(0), header
        )
        return status, header.raw + body

    def end(self, request_id: bytes) -> tuple[int, bytes]:
        header = ctypes.create_string_buffer(HEADER_SIZE)
        status = self._c.rf_end(self._peer, _id(request_id), header)
        return status, header.raw

    def refuse(self, request_id: bytes) -> tuple[int, bytes]:
        header = ctypes.create_string_buffer(HEADER_SIZE)
        status = self._c.rf_refuse(self._peer, _id(request_id), ctypes.c_uint8(END_BUSY), header)
        return status, header.raw

    def reply_again(self, request_id: bytes, number: int, body: bytes) -> tuple[int, bytes]:
        header = ctypes.create_string_buffer(HEADER_SIZE)
        status = self._c.rf_reply_again(
            self._peer,
            _id(request_id),
            ctypes.c_uint64(number),
            ctypes.c_size_t(len(body)),
            ctypes.c_uint8(0),
            header,
        )
        return status, header.raw + body

    def end_again(self, request_id: bytes) -> tuple[int, bytes]:
        header = ctypes.create_string_buffer(HEADER_SIZE)
        status = self._c.rf_end_again(self._peer, _id(request_id), header)
        return status, header.raw


def _id(request_id: bytes) -> _Id:
    return _Id((ctypes.c_uint8 * 16)(*request_id))
