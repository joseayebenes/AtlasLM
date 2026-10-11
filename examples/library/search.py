#!/usr/bin/env python3
"""Search and cited answers over the sample documents, with the model and embeddings of a
configuration (Ollama with atlas.dev.toml).

    python examples/library/search.py [atlas.dev.toml] [QUESTION]

The configuration says where the documents are and which model to use; `atlas run`
processes and indexes them (only what changed since the last time), and then atlas.index
searches and answers.
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.cli import run  # noqa: E402
from atlas.config import load_config  # noqa: E402
from atlas.index import Index, cite  # noqa: E402


def main(config: Path, question: str = "When does the HMI raise an alarm?") -> None:
    cfg = load_config(config)
    with contextlib.redirect_stdout(io.StringIO()):          # the report of atlas run, as JSON
        if run(cfg) != 0:
            sys.exit(f"atlas run {config} did not finish well: see atlas status {config}")
    idx = Index.from_config(cfg)
    for hit in idx.search("track deletion latency", k=3):
        print(f"{cite(hit)}  ({hit['kind']})")
    res = idx.answer(question)
    print(f"\n{question}\n{res['answer']}")
    for n, source in enumerate(res["sources"], 1):
        print(f"  [{n}] {source['cite']}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "atlas.dev.toml"), *sys.argv[2:3])
