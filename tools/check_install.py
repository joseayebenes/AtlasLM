#!/usr/bin/env python3
"""Clean installation check: the steps of docs/instalacion.md, run in a new folder.

    python tools/check_install.py WHEELHOUSE [--python PYTHON] [--keep]

1. a new virtual environment with PYTHON (by default, the one running this script);
2. `pip install --no-index --find-links WHEELHOUSE "atlaslm[all]" hephestolm`: no network, only
   the wheelhouse (make wheelhouse builds it for this machine);
3. `atlas --version`, `hephesto --version` and `atlas init` in a new project folder;
4. the sample data (examples/samples) as the project's documents, code and requirements,
   and the template edited to use them and the fake API (tests/fakes/fake_api.py);
5. `atlas check`, `atlas run` twice (the second one must process nothing), `atlas status`,
   `atlas trace` and `atlas search`.

It prints each command and stops at the first failure. The folder is deleted at the end,
unless --keep.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def step(cmd: list[str], cwd: Path, env: dict, check=lambda out: True) -> str:
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    res = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, capture_output=True, text=True)
    out = res.stdout + res.stderr
    if res.returncode != 0 or not check(res.stdout):
        print(out)
        sys.exit(f"FAILED: {' '.join(str(c) for c in cmd)} (exit {res.returncode})")
    print("  " + "\n  ".join(res.stdout.strip().splitlines()[-3:]), flush=True)
    return res.stdout


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wheelhouse", type=Path)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    wheelhouse = args.wheelhouse.resolve()
    if not any(wheelhouse.glob("atlaslm-*.whl")):
        sys.exit(f"{wheelhouse} has no atlaslm wheel: make wheelhouse")

    base = Path(tempfile.mkdtemp(prefix="atlas-install-"))
    venv, project = base / "venv", base / "project"
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV", "ATLAS_API_KEY")}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    fake = None
    try:
        step([args.python, "-m", "venv", venv], base, env)
        step([bin_dir / "python", "-m", "pip", "install", "-q", "--no-index", "--find-links", wheelhouse,
              "atlaslm[all]", *(["hephestolm"] if any(wheelhouse.glob("hephestolm-*.whl")) else [])], base, env)
        step([bin_dir / "atlas", "--version"], base, env)
        if any(wheelhouse.glob("hephestolm-*.whl")):
            step([bin_dir / "hephesto", "--version"], base, env)
        project.mkdir()
        step([bin_dir / "atlas", "init"], project, env)

        samples = ROOT / "examples" / "samples"
        if not (samples / "docs" / "spec_with_bookmarks.pdf").exists():
            subprocess.run([sys.executable, str(ROOT / "examples" / "make_samples.py")], check=True,
                           capture_output=True)
        shutil.copytree(samples / "docs", project / "docs")
        shutil.copytree(samples / "ada", project / "src")
        shutil.copy(samples / "requirements.sqlite", project / "requirements.sqlite")

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        (base / "faults.json").write_text("{}")
        fake = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fakes" / "fake_api.py"), "--port", str(port),
                                 "--control", str(base / "faults.json")], stdout=subprocess.DEVNULL)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2)
                break
            except OSError:
                time.sleep(0.1)

        toml = project / "atlas.toml"
        text = toml.read_text(encoding="utf-8")
        for old, new in (('base_url = "http://localhost:11434/v1"', f'base_url = "http://127.0.0.1:{port}/v1"'),
                         ('model = "gemma4:12b"', 'model = "fake"'),
                         ("enabled = false", "enabled = true"),
                         ('id_pattern = "^(?:SRS|SSS)-\\\\d{3,6}$"', 'id_pattern = "^(?:SRS|SSS)-\\\\d{4}$"')):
            if old not in text:
                sys.exit(f"FAILED: the template no longer has {old!r}; update this check")
            text = text.replace(old, new)
        toml.write_text(text, encoding="utf-8")

        step([bin_dir / "atlas", "check", "atlas.toml"], project, env)
        step([bin_dir / "atlas", "run", "atlas.toml"], project, env)
        step([bin_dir / "atlas", "run", "atlas.toml"], project, env,
             check=lambda out: json.loads(out)["processed"] == {})
        step([bin_dir / "atlas", "status", "atlas.toml"], project, env, check=lambda out: "last run:" in out)
        step([bin_dir / "atlas", "trace", "SRS-0004", "atlas.toml"], project, env,
             check=lambda out: "SRS-0004" in out)
        step([bin_dir / "atlas", "search", "track deletion", "atlas.toml"], project, env)
        print(f"\nOK: clean installation from {wheelhouse} with {args.python}")
    finally:
        if fake:
            fake.terminate()
            fake.wait(timeout=5)
        if args.keep:
            print(f"kept in {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
