"""Builds ``rulfo._core``, the C core as a Python extension module (cffi, API mode).

hatchling runs :class:`CoreBuildHook` for every wheel, the editable one included. The module uses
the stable ABI of CPython, so one build serves CPython 3.12 and later. It is not built for
free-threaded Python.

Run as a script to build into another directory, with ``--sanitize`` for AddressSanitizer and
UndefinedBehaviorSanitizer::

    python hatch_build.py build/asan/rulfo --sanitize
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any

from cffi import FFI
from hatchling.builders.hooks.plugin.interface import BuildHookInterface

ROOT = Path(__file__).resolve().parent
CORE = ROOT / "core"
MODULE = "_core.abi3.so"
# The oldest CPython the module is built for, as a wheel tag.
PYTHON_TAG = "cp312"
SANITIZERS = ["-fsanitize=address,undefined", "-fno-sanitize-recover=all", "-g"]


def declarations(header: str) -> str:
    """``rulfo.h`` in the form cffi reads: no comments, no preprocessor lines but constants.

    A constant that is not a decimal number is declared as ``...``: the C compiler gives its value.
    """
    text = re.sub(r"/\*.*?\*/", "", header, flags=re.DOTALL)
    text = re.sub(r"#ifdef __cplusplus.*?#endif", "", text, flags=re.DOTALL)
    lines: list[str] = []
    for line in text.splitlines():
        define = re.match(r"#define\s+(\w+)\s+(.+)", line)
        if define:
            name, value = define[1], define[2].strip()
            lines.append(f"#define {name} {value if value.isdecimal() else '...'}")
        elif not line.startswith("#"):
            lines.append(line)
    return "\n".join(lines)


def compile_core(out: Path, *, sanitize: bool = False) -> Path:
    """Compiles the module into ``out`` and returns its path."""
    if sys.platform != "linux":
        msg = "rulfo is built only for Linux"
        raise RuntimeError(msg)
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        msg = "rulfo is not built for free-threaded Python"
        raise RuntimeError(msg)

    builder = FFI()
    builder.cdef(declarations((CORE / "include" / "rulfo.h").read_text()))
    builder.set_source("rulfo._core", '#include "rulfo.h"')
    generated = ROOT / "build" / "ffi" / "_core.c"
    generated.parent.mkdir(parents=True, exist_ok=True)
    builder.emit_c_code(str(generated))

    out.mkdir(parents=True, exist_ok=True)
    module = out / MODULE
    compiler = shlex.split(os.environ.get("CC", "cc"))
    command = [*compiler, "-shared", "-fPIC", "-O2", "-std=c11", "-fvisibility=hidden"]
    command += SANITIZERS if sanitize else []
    command += [f"-I{CORE / 'include'}", f"-I{sysconfig.get_paths()['include']}"]
    command += [str(generated), *sorted(str(path) for path in (CORE / "src").glob("*.c"))]
    command += ["-o", str(module)]
    subprocess.run(command, check=True)  # noqa: S603
    return module


class CoreBuildHook(BuildHookInterface[Any]):
    """Puts ``rulfo._core`` into the wheel and tags the wheel for this platform."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:  # noqa: ARG002
        module = compile_core(ROOT / "src" / "rulfo")
        platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        build_data["pure_python"] = False
        build_data["tag"] = f"{PYTHON_TAG}-abi3-{platform}"
        build_data["artifacts"].append(module.relative_to(ROOT).as_posix())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--sanitize", action="store_true")
    arguments = parser.parse_args()
    print(compile_core(arguments.out.resolve(), sanitize=arguments.sanitize))  # noqa: T201


if __name__ == "__main__":
    main()
