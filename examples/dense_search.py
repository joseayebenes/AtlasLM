#!/usr/bin/env python3
"""Times the dense search of the index of `make large` (examples/large_project.py):

    python examples/dense_search.py [DEST] [--dim 768]

- cold: the first search of a process, which loads the vectors of atlas.db into a matrix;
- warm: the next searches, with the matrix already loaded (with and without a filter by
  document);
- with a writer: the searches while another process commits to the ledger of the same
  atlas.db every 50 ms, as an `atlas run` does while the MCP server answers.

The queries are embedded here, without a server, the way tests/fakes/fake_api.py does
(64 dimensions, those of the index of `make large`). With --dim N, a copy of atlas.db with
every vector repeated up to N dimensions (768: embeddinggemma) is measured instead. The
times are wall seconds, the median of each series.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.index import Embedder, Index  # noqa: E402

QUERIES = ["track deletion latency", "process track event within ms", "operator screen tracks",
           "SRS-00012 deadline", "reception of a track event", "function group 3.2 track handling"]

WRITER = """
import sys, time
from atlas.pipeline import Ledger
ledger = Ledger(sys.argv[1])
n = 0
while True:
    ledger.mark("bench.writer", f"unit-{n % 10}", "ok")
    n += 1
    time.sleep(0.05)
"""


class LocalEmbedder(Embedder):
    """The vectors of tests/fakes/fake_api.py, `dim` long, normalised as Embedder does."""

    def __init__(self, model: str, dim: int):        # no client: nothing goes to a server
        self.model, self.dim, self.batch = model, dim, 64

    def queries(self, texts: list[str]) -> np.ndarray:
        m = np.zeros((len(texts), 64), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"\w+", t.lower()):
                m[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
        m = np.tile(m, (1, -(-self.dim // 64)))[:, :self.dim]
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(norms == 0, 1, norms)


def widened(db: Path, dim: int) -> Path:
    """A copy of atlas.db with every vector repeated up to `dim` dimensions."""
    copy = Path(tempfile.mkdtemp()) / "atlas.db"
    shutil.copy(db, copy)
    con = sqlite3.connect(copy)
    rows = con.execute("SELECT id, vector FROM index_records").fetchall()
    con.executemany("UPDATE index_records SET vector = ? WHERE id = ?",
                    [(np.tile(np.frombuffer(v, np.float32), -(-dim // 64))[:dim].tobytes(), i) for i, v in rows])
    con.commit()
    con.execute("VACUUM")
    con.close()
    return copy


def median(f, times: int, pause: float = 0) -> float:
    out = []
    for i in range(times):
        t = time.perf_counter()
        f(i)
        out.append(time.perf_counter() - t)
        time.sleep(pause)
    return statistics.median(out)


def main(dest: Path, dim: int) -> None:
    db = dest / "workspace" / "atlas.db"
    if dim != 64:
        db = widened(db, dim)
    try:
        measure(db, dim)
    finally:
        if dim != 64:
            shutil.rmtree(db.parent)


def measure(db: Path, dim: int) -> None:
    model = sqlite3.connect(db).execute("SELECT embed_model FROM index_documents LIMIT 1").fetchone()[0]
    embedder = LocalEmbedder(model, dim)

    def fresh() -> Index:
        return Index(db, embedder)

    idx = fresh()
    print(f"{idx.store.counts()['records']} records of {dim} dimensions in {db}")
    q = lambda i: QUERIES[i % len(QUERIES)]
    results = {}
    results["cold: first search"] = median(lambda i: fresh().search(q(i), dense=True), 5)
    results["cold: loading the matrix"] = median(lambda i: fresh().store._load_matrix(model), 5)
    idx.search(q(0), dense=True)
    results["warm: search"] = median(lambda i: idx.search(q(i), dense=True), 30)
    results["warm: search, one document"] = median(lambda i: idx.search(q(i), document="spec_03.pdf", dense=True), 30)
    results["warm: search without vectors"] = median(lambda i: idx.search(q(i), dense=False), 30)
    qvec = embedder.queries([q(0)])[0]
    results["warm: dense alone"] = median(lambda i: idx.store.dense(qvec, model, 24, None), 50)
    allowed = idx.store.allowed("spec_03.pdf")
    results["warm: dense alone, one document"] = median(lambda i: idx.store.dense(qvec, model, 24, allowed), 50)
    writer = subprocess.Popen([sys.executable, "-c", WRITER, str(db)], cwd=ROOT)
    try:
        time.sleep(1)
        results["with a writer: search"] = median(lambda i: idx.search(q(i), dense=True), 20, pause=0.06)
    finally:
        writer.terminate()
        writer.wait()
        sqlite3.connect(db).execute("DELETE FROM ledger WHERE step = 'bench.writer'").connection.commit()
    width = max(map(len, results))
    for k, v in results.items():
        print(f"  {k:<{width}}  {v * 1000:8.1f} ms")


if __name__ == "__main__":
    args = sys.argv[1:]
    dim = 64
    if "--dim" in args:
        dim = int(args[args.index("--dim") + 1])
        del args[args.index("--dim"):args.index("--dim") + 2]
    main(Path(args[0]) if args else ROOT / "examples" / "large", dim)
