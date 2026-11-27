"""The quickstart of the README runs and prints what the README shows."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"


def _block(text: str, language: str) -> str:
    found = re.search(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)
    assert found is not None
    return found[1]


def test_the_quickstart_runs() -> None:
    text = README.read_text()
    quickstart = text[text.index("## Quickstart") :]
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _block(quickstart, "python")],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert done.stdout == _block(quickstart, "text")
