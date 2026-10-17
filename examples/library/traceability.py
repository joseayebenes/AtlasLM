#!/usr/bin/env python3
"""Traceability without a model: the samples (requirements, Ada code and the specification
with its requirements found by regex) processed into one graph, and the questions of
atlas.graph and atlas.agents.build_context over it.

    python examples/library/traceability.py [WORKDIR]
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.agents import build_context  # noqa: E402
from atlas.cli import run  # noqa: E402
from atlas.config import load_config  # noqa: E402
from atlas.graph import KnowledgeGraph  # noqa: E402

SAMPLES = ROOT / "examples" / "samples"


def config(workdir: Path) -> Path:
    path = workdir / "atlas.toml"
    path.write_text(f'''
[project]
workspace = "{(workdir / 'workspace').as_posix()}"
log_level = "WARNING"
[pipeline]
steps = ["doors", "code", "docs", "graph"]
[[sources.docs]]
name = "standards"
paths = ["{(SAMPLES / 'docs' / 'spec_with_bookmarks.pdf').as_posix()}"]
analysis = "regex"
[sources.doors]
adapter = "exchange"
modules = ["SSS_MISSION", "SRS_TMS"]
[sources.doors.exchange]
path = "{(SAMPLES / 'requirements.sqlite').as_posix()}"
[sources.code]
roots = ["{(SAMPLES / 'ada').as_posix()}"]
test_patterns = ["**/tests/**"]
''', encoding="utf-8")
    return path


def main(workdir: Path) -> None:
    if not (SAMPLES / "requirements.sqlite").exists():
        sys.exit("first: python examples/make_samples.py")
    cfg = load_config(config(workdir))
    with contextlib.redirect_stdout(io.StringIO()):          # the report of atlas run, as JSON
        assert run(cfg) == 0
    g = KnowledgeGraph.from_config(cfg)
    t = g.trace("SRS-0004")
    print(f"SRS-0004 ({t['module']}): {t['text']}")
    print("  implemented by:", [c["id"] for c in t["implemented_by"]["items"]],
          "verified by:", [x["id"] for x in t["verified_by"]])
    for d in t["documents"]:                                  # DOORS and the document disagree?
        print(f"  {d['document']} § {d['title']}: {d['text']}  (values that differ: {d['values_differ']})")
    affected = g.impact("Delete_Track")
    print("changing Delete_Track affects requirements", [r["id"] for r in affected["requirements"]])
    print("SRS_TMS:", g.matrix("SRS_TMS")["summary"])
    package = build_context(g, "SRS-0004", budget_tokens=800)
    print(f"context package of SRS-0004: {package['tokens']} tokens")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="atlas-trace-")))
