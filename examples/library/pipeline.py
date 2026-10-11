#!/usr/bin/env python3
"""The incremental engine on its own: a step of our own over the Ada samples.

    python examples/library/pipeline.py [WORKDIR]

The first run processes every file; the second one, none (the fingerprints did not change);
after touching one file, only that one. Each run leaves its time profile in the ledger.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.pipeline import Ledger, Runner, Step, atomic_write, sha256_file  # noqa: E402

SOURCES = ROOT / "examples" / "samples" / "ada"


def lines_step(out: Path) -> Step:
    """One unit per Ada file: its number of lines, written next to the others."""
    def run(ctx, f: str) -> dict:
        n = len(Path(f).read_text(encoding="utf-8").splitlines())
        atomic_write(out / f"{Path(f).name}.lines", str(n))          # idempotent and atomic
        return {"lines": n}

    return Step(name="lines",
                units=lambda ctx: sorted(str(p) for p in SOURCES.rglob("*.ad[bs]")),
                fingerprint=lambda ctx, f: sha256_file(f),
                run=run,
                prune=lambda ctx, f: (out / f"{Path(f).name}.lines").unlink(missing_ok=True))


def main(workdir: Path) -> None:
    if not SOURCES.exists():
        sys.exit("first: python examples/make_samples.py")
    ledger = Ledger(workdir / "atlas.db")
    runner = Runner(ledger, [lines_step(workdir / "lines")], workers=4)
    first, second = runner.run(), runner.run()
    print(f"first run: {first.processed}; second run: {second.processed}, up to date {second.skipped}")
    profile = ledger.runs(2)[1]["report"]["profile"]["lines"]
    print(f"profile of the first one: {profile['units']} units in {profile['seconds']:.3f} s, "
          f"slowest {Path(profile['slowest'][0]['unit']).name}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="atlas-pipeline-")))
