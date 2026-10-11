#!/usr/bin/env python3
"""Merges the `pip freeze` of the same installation on several Python versions into one
constraints file, with a marker where the versions differ.

    python tools/constraints.py freeze-3.10.txt:3.10 freeze-3.11.txt:3.11 ... > constraints.txt

Each freeze comes from a clean virtual environment with `atlaslm[all,dev]` installed (from
the wheel) in which the tests passed (see docs/instalacion.md, "Versiones fijadas"). A
package whose version is the same in all of them gets one line; otherwise one line per run
of consecutive Python versions, `python_version >= "3.11" and python_version < "3.12"`.
The last run has no upper bound, so a newer Python still gets the newest pins.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # the atlas of this repository

HEADER = """\
# Versiones exactas con las que pasan las pruebas de atlaslm {version} (instalación limpia
# de atlaslm[all,dev] en Python {pythons}, {today}). Uso:
#     pip install "atlaslm[all]" -c constraints.txt
# Generado con tools/constraints.py; no se edita a mano.
"""


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def read_freeze(path: str) -> dict[str, str]:
    pins = {}
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^([A-Za-z0-9_.\-]+)==(\S+)$", line.strip())
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def merge(freezes: dict[str, dict[str, str]]) -> list[str]:
    """{python: {package: version}} -> constraint lines."""
    pythons = sorted(freezes, key=_key)
    lines = []
    for pkg in sorted({p for f in freezes.values() for p in f}):
        versions = [freezes[py].get(pkg) for py in pythons]
        if all(v == versions[0] for v in versions) and versions[0]:
            lines.append(f"{pkg}=={versions[0]}")
            continue
        start = 0
        for i in range(1, len(pythons) + 1):
            if i < len(pythons) and versions[i] == versions[start]:
                continue
            if versions[start]:
                bounds = []
                if start > 0:
                    bounds.append(f'python_version >= "{pythons[start]}"')
                if i < len(pythons):
                    bounds.append(f'python_version < "{pythons[i]}"')
                lines.append(f"{pkg}=={versions[start]}; {' and '.join(bounds)}")
            start = i
    return lines


def main(args: list[str]) -> None:
    if not args:
        sys.exit(__doc__)
    freezes = {}
    for arg in args:
        path, _, python = arg.rpartition(":")
        freezes[python] = read_freeze(path)
    from atlas import __version__
    pythons = ", ".join(sorted(freezes, key=_key))
    print(HEADER.format(version=__version__, pythons=pythons, today=date.today().isoformat()), end="")
    print("\n".join(merge(freezes)))


if __name__ == "__main__":
    main(sys.argv[1:])
