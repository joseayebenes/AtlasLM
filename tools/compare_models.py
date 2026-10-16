#!/usr/bin/env python3
"""Compares chat models on the samples: `atlas run` and `atlas eval` with each one, and a
table with how many answers of the agents each model got right and how long it took.

    python tools/compare_models.py MODEL [MODEL...] [--config atlas.openrouter.toml] [--no-run] [--out FILE]

Each model goes to atlas in the ATLAS_MODEL variable. atlas.openrouter.toml gives each one its
own workspace (workspace-openrouter/<model>): a model already processed is not processed
again, and its reports stay there. ATLAS_REASONING, if set, goes to every model. --no-run
only builds the table from the reports already written.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.config import load_config  # noqa: E402

HEADER = ("| model | right | median (s) | total (s) | errors | failed cases |\n"
          "|---|---|---|---|---|---|")


def results(config: Path, model: str) -> list[dict]:
    """The answers of the agents in the eval reports of the model, one per case and run."""
    os.environ["ATLAS_MODEL"] = model
    cfg = load_config(config)
    found = []
    for s in (cfg["agents"].get("eval") or {}).get("sets", []):
        report = cfg.workspace / "reports" / f"eval-{Path(s).stem}.json"
        if report.exists():
            found += json.loads(report.read_text(encoding="utf-8")).get("agents", {}).get("results", [])
    return found


def row(model: str, answers: list[dict], failure: str = "") -> str:
    if failure or not answers:
        return f"| `{model}` | {failure or 'no report'} | | | | |"
    seconds = [r["seconds"] for r in answers]
    failed = sorted({r["id"] for r in answers if not r["passed"]})
    return (f"| `{model}` | {sum(r['passed'] for r in answers)}/{len(answers)} | {statistics.median(seconds):.0f} "
            f"| {sum(seconds):.0f} | {sum(1 for r in answers if r.get('error'))} | {', '.join(failed) or '-'} |")


def evaluate(config: Path, model: str) -> str:
    """atlas run and atlas eval with the model; what failed, or "" if both ran. A run with
    units that failed (exit code 1) still leaves an index to ask; a fatal error (2) does not."""
    env = {**os.environ, "ATLAS_MODEL": model}
    for action in ("run", "eval"):
        print(f"== {model}: atlas {action}", file=sys.stderr, flush=True)
        code = subprocess.run([sys.executable, "-m", "atlas.cli", action, str(config)], env=env,
                              stdout=subprocess.DEVNULL if action == "run" else None).returncode
        if code > (1 if action == "run" else 0):
            return f"atlas {action} failed ({code})"
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Compares chat models with atlas run and atlas eval.")
    ap.add_argument("models", nargs="+", help="models, as the server names them (e.g. qwen/qwen3.6-35b-a3b)")
    ap.add_argument("--config", type=Path, default=ROOT / "atlas.openrouter.toml",
                    help="configuration that takes the model from ATLAS_MODEL (default atlas.openrouter.toml)")
    ap.add_argument("--no-run", action="store_true", help="only the table, from the reports already written")
    ap.add_argument("--out", type=Path, help="also write the table to this Markdown file")
    args = ap.parse_args()
    config = args.config.resolve()
    rows = []
    for model in args.models:
        failure = "" if args.no_run else evaluate(config, model)
        rows.append(row(model, results(config, model), failure))
    table = "\n".join([HEADER, *rows])
    print(f"\n{table}")
    if args.out:
        args.out.write_text(table + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
