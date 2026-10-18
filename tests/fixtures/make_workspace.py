"""Generates tests/fixtures/atlas-<version>.db: the atlas.db that a released atlas leaves
after a run over its samples, for test_db.py to open and migrate.

    python tests/fixtures/make_workspace.py v0.1.0

It checks the tag out in a temporary git worktree, generates its samples, starts its fake
API, runs it with its atlas.test.toml (without the PDFs, so that the file stays small) and
compacts the result. Only for a new SCHEMA_VERSION: see tests/fixtures/README.md.
"""
from __future__ import annotations

import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(tag: str) -> None:
    out = ROOT / "tests" / "fixtures" / f"atlas-{tag.lstrip('v')}.db"
    with tempfile.TemporaryDirectory() as tmp:
        src, ws = Path(tmp) / "src", Path(tmp) / "ws"
        subprocess.run(["git", "worktree", "add", "--detach", str(src), tag], cwd=ROOT, check=True)
        try:
            subprocess.run([sys.executable, "examples/make_samples.py"], cwd=src, check=True)
            sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
            (Path(tmp) / "faults.json").write_text("{}")
            fake = subprocess.Popen([sys.executable, "tests/fakes/fake_api.py", "--port", str(port),
                                     "--control", str(Path(tmp) / "faults.json")], cwd=src)
            try:
                for _ in range(50):
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2); break
                    except Exception:
                        time.sleep(0.1)
                toml = (src / "atlas.test.toml").read_text(encoding="utf-8")
                toml = toml.replace("./workspace-test", ws.as_posix()).replace("127.0.0.1:8770", f"127.0.0.1:{port}")
                toml, pdfs = re.subn(r'\[\[sources\.docs\]\]\nname = "normas".*?(?=\[\[sources\.docs\]\])', "",
                                     toml, flags=re.S)
                assert pdfs == 1, "the PDFs source of atlas.test.toml was not found"
                (Path(tmp) / "atlas.toml").write_text(toml, encoding="utf-8")
                subprocess.run([sys.executable, "-m", "atlas.cli", "run", str(Path(tmp) / "atlas.toml")],
                               cwd=src, check=True, stdout=subprocess.DEVNULL)
            finally:
                fake.terminate(); fake.wait(timeout=5)
            db = sqlite3.connect(ws / "atlas.db")
            db.execute("VACUUM")
            db.close()
            out.write_bytes((ws / "atlas.db").read_bytes())
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(src)], cwd=ROOT, check=True)
    print(f"created {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "v0.1.0")
