"""Every file in examples/ runs, and prints what the docs page that includes it shows."""

from __future__ import annotations

import ast
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
DOCS = ROOT / "docs"
WAIT = 60  # seconds: the machine that runs the tests can be busy


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _shown(page: str, after: str) -> str:
    """The first ``text`` block of a docs page that follows the words ``after``."""
    text = (DOCS / page).read_text()
    found = re.search(r"```text\n(.*?)```", text[text.index(after) :], flags=re.DOTALL)
    assert found is not None
    return found[1]


def _without_retries(output: str) -> str:
    """``output`` without the lines whose number depends on how long a restart takes."""
    lines = output.splitlines(keepends=True)
    return "".join(line for line in lines if not line.startswith("[client] no connection"))


class _Program:
    """An example that runs as a process. Its output is read while it runs."""

    def __init__(self, *command: str, env: dict[str, str] | None = None) -> None:
        self._process = subprocess.Popen(  # noqa: S603
            [sys.executable, *command],
            cwd=ROOT,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._lines: list[str] = []
        self._errors: list[str] = []
        self._readers = [
            threading.Thread(target=self._read, args=(self._process.stdout, self._lines)),
            threading.Thread(target=self._read, args=(self._process.stderr, self._errors)),
        ]
        for reader in self._readers:
            reader.start()

    @staticmethod
    def _read(stream: object, lines: list[str]) -> None:
        lines.extend(stream)  # type: ignore[arg-type]

    @property
    def errors(self) -> str:
        return "".join(self._errors)

    def wait_for(self, words: str) -> None:
        """Returns when a line of the output has ``words``."""
        limit = time.monotonic() + WAIT
        while not any(words in line for line in self._lines):
            assert self._process.poll() is None, f"ended before {words!r}: {self.errors}"
            assert time.monotonic() < limit, f"no {words!r} in {self._lines}"
            time.sleep(0.05)

    def output(self) -> str:
        """Waits for the end of the program, and returns what it printed."""
        try:
            code = self._process.wait(timeout=WAIT)
        finally:
            self._process.kill()
            for reader in self._readers:
                reader.join()
        assert code == 0, self.errors
        return "".join(self._lines)

    def stop(self) -> str:
        """Sends what Ctrl-C sends, and returns what the program printed."""
        self._process.send_signal(signal.SIGINT)
        return self.output()


def _example(name: str, *arguments: str, env: dict[str, str] | None = None) -> _Program:
    return _Program(str(EXAMPLES / name), *arguments, env=env)


def test_the_quickstart_server_and_client_print_what_the_page_shows() -> None:
    port = _free_port()
    server = _example("server.py", str(port))
    try:
        server.wait_for("listening")
        client = _example("client.py", str(port)).output()
        server.wait_for("disconnected")
    finally:
        served = server.stop()
    assert client == _shown("quickstart.md", "Terminal 2 prints")
    assert served.replace(str(port), "7373") == _shown("quickstart.md", "Terminal 1 prints")


def test_a_request_completes_when_the_server_restarts_before_the_deadline() -> None:
    port = _free_port()
    first = _example("server.py", str(port))
    client = None
    try:
        first.wait_for("listening")
        client = _example("slow_client.py", str(port))
        first.wait_for("slow starts")
    finally:
        first_output = first.stop()
    second = _example("server.py", str(port))
    try:
        client_output = client.output()
        second.wait_for("disconnected")
    finally:
        second_output = second.stop()

    page = "quickstart.md"
    shown = _shown(page, "The client connects again")
    assert _without_retries(client_output.replace(str(port), "7373")) == _without_retries(shown)
    assert first_output.replace(str(port), "7373") == _shown(page, "The first server printed")
    assert second_output.replace(str(port), "7373") == _shown(page, "The second server prints")


def test_a_request_expires_when_the_server_stays_stopped() -> None:
    port = _free_port()
    server = _example("server.py", str(port))
    client = None
    try:
        server.wait_for("listening")
        client = _example("slow_client.py", str(port))
        server.wait_for("slow starts")
    finally:
        server.stop()
    shown = _shown("quickstart.md", "Do not start the server again")
    assert _without_retries(client.output()) == _without_retries(shown)


def test_the_outcomes_example_prints_what_the_page_shows() -> None:
    program = _example("outcomes.py")
    assert program.output() == _shown("python.md", "It prints")
    assert "the handler of 'broken' failed" in program.errors
    assert "ValueError: cannot read b'x'" in program.errors


def test_the_takeover_example_prints_what_the_page_shows() -> None:
    assert _example("takeover.py").output() == _shown("python.md", "The whole example prints")


def _server_and_workers(env: dict[str, str]) -> None:
    port = _free_port()
    server = _example("auth_server.py", str(port), env=env)
    workers: list[_Program] = []
    try:
        server.wait_for("listening")
        refused = _example("worker.py", "worker-1", "wrong", str(port), env=env).output()
        server.wait_for("refused")
        for number in (1, 2):
            name = f"worker-{number}"
            workers.append(_example("worker.py", name, f"token-{number}", str(port), env=env))
            server.wait_for(f"{name} passed")
        served = server.output()
    finally:
        outputs = [worker.output() for worker in workers]
    assert served.replace(str(port), "7374") == _shown("server.md", "The server prints")
    assert outputs[0] == _shown("server.md", "worker-1 prints")
    assert refused == _shown("server.md", "wrong token prints")


def test_the_server_and_worker_examples_print_what_the_page_shows() -> None:
    _server_and_workers({})


def test_the_server_and_worker_examples_run_over_tls(tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("needs the openssl program for a certificate")
    certificate, key = tmp_path / "server.crt", tmp_path / "server.key"
    command = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1"]
    command += ["-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1"]
    command += ["-keyout", str(key), "-out", str(certificate)]
    subprocess.run(command, check=True, capture_output=True)  # noqa: S603
    _server_and_workers(
        {"RULFO_CERT": str(certificate), "RULFO_KEY": str(key), "RULFO_CA": str(certificate)}
    )


def test_the_tests_of_the_testing_page_pass() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", str(EXAMPLES / "test_add.py"), "-o", "addopts="],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=WAIT,
        check=False,
    )
    assert "3 passed" in done.stdout, done.stdout + done.stderr


def test_the_shared_websocket_example_prints_what_the_page_shows() -> None:
    pytest.importorskip("websockets")
    assert _example("shared_websocket.py").output() == _shown("transports.md", "It prints")


def test_the_litestar_server_and_the_websockets_client_exchange_a_request() -> None:
    for module in ("litestar", "uvicorn", "websockets"):
        pytest.importorskip(module)
    port = _free_port()
    arguments = ["--app-dir", str(EXAMPLES), "litestar_server:app", "--port", str(port)]
    server = _Program("-m", "uvicorn", *arguments)
    try:
        # uvicorn logs to stderr, so wait for the port.
        limit = time.monotonic() + WAIT
        while True:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            assert time.monotonic() < limit, server.errors
            time.sleep(0.1)
        url = f"ws://127.0.0.1:{port}/rulfo/client-1"
        client = _example("websocket_client.py", url).output()
    finally:
        server.stop()
    assert client == _shown("transports.md", "The client prints")


def test_every_example_is_run_by_a_test() -> None:
    tests = Path(__file__).read_text()
    for example in sorted(EXAMPLES.glob("*.py")):
        assert f'"{example.name}"' in tests or f"{example.stem}:app" in tests, example.name


def test_every_python_block_of_the_docs_is_an_example_file_or_parses() -> None:
    """A block is a file of examples/, which a test runs, or a fragment that at least parses."""
    for page in sorted(DOCS.glob("*.md")):
        for block in re.findall(r"```python\n(.*?)```", page.read_text(), flags=re.DOTALL):
            if block.startswith('--8<-- "examples/'):
                name = block.split('"')[1].split(":")[0]
                assert (ROOT / name).is_file(), f"{page.name}: {name}"
            else:
                flags = ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
                compile(block, page.name, "exec", flags=flags)  # raises SyntaxError
