#!/usr/bin/env python3
"""Compares chat models on the samples: `atlas run` and `atlas eval` with each one, and a
table with how many answers of the agents each model got right and how long it took.

    python tools/compare_models.py MODEL [MODEL...] [--config atlas.openrouter.toml] [--no-run] [--out FILE]
                                   [--develop examples/eval/develop.yaml]

Each model goes to atlas in the ATLAS_MODEL variable. atlas.openrouter.toml gives each one its
own workspace (workspace-openrouter/<model>): a model already processed is not processed
again, and its reports stay there. ATLAS_REASONING, if set, goes to every model. --no-run
only builds the table from the reports already written. --develop SET also runs that set of
develop cases (atlas eval --set SET: plans of atlas develop, minutes or hours each) and adds a
second table: the tests passing at the end, the edits that failed, the compilations with errors,
the calls, the time and the cost of each model.
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
DEVELOP_HEADER = ("| model | runs that reached their tests | tests passing | edits (failed) | compilations with "
                  "errors | calls | min | cost |\n|---|---|---|---|---|---|---|---|")


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


def develop_results(config: Path, model: str, develop_set: Path) -> list[dict]:
    """The runs of the develop cases in the eval report of `develop_set` of the model."""
    os.environ["ATLAS_MODEL"] = model
    report = load_config(config).workspace / "reports" / f"eval-{develop_set.stem}.json"
    if not report.exists():
        return []
    return json.loads(report.read_text(encoding="utf-8")).get("develop", {}).get("results", [])


def develop_row(model: str, runs: list[dict], failure: str = "") -> str:
    if failure or not runs:
        return f"| `{model}` | {failure or 'no report'} | | | | | | |"
    edits = lambda k: sum((r.get("edits") or {}).get(k, 0) for r in runs)
    cost = sum(r.get("cost", 0) for r in runs)
    return (f"| `{model}` | {sum(r['passed'] for r in runs)}/{len(runs)} | "
            f"{sum(r.get('tests_passed', 0) for r in runs)}/{sum(r.get('tests', 0) for r in runs)} | "
            f"{edits('made') + edits('failed')} ({edits('failed')}) | "
            f"{sum((r.get('compiles') or {}).get('failed', 0) for r in runs)} | {sum(r.get('calls', 0) for r in runs)} | "
            f"{sum(r['seconds'] for r in runs) / 60:.1f} | " + (f"${cost:.2f}" if any("cost" in r for r in runs) else "-")
            + " |")


def row(model: str, answers: list[dict], failure: str = "") -> str:
    if failure or not answers:
        return f"| `{model}` | {failure or 'no report'} | | | | |"
    seconds = [r["seconds"] for r in answers]
    failed = sorted({r["id"] for r in answers if not r["passed"]})
    return (f"| `{model}` | {sum(r['passed'] for r in answers)}/{len(answers)} | {statistics.median(seconds):.0f} "
            f"| {sum(seconds):.0f} | {sum(1 for r in answers if r.get('error'))} | {', '.join(failed) or '-'} |")


def evaluate(config: Path, model: str, develop_set: Path | None = None) -> tuple[str, str]:
    """atlas run and atlas eval with the model, and the develop set if given: (what failed of
    the first two, what failed of the develop set), "" for what ran. A run with units that
    failed (exit code 1) still leaves an index to ask; a fatal error (2) does not."""
    env = {**os.environ, "ATLAS_MODEL": model}
    for action in ("run", "eval"):
        print(f"== {model}: atlas {action}", file=sys.stderr, flush=True)
        code = subprocess.run([sys.executable, "-m", "atlas.cli", action, str(config)], env=env,
                              stdout=subprocess.DEVNULL if action == "run" else None).returncode
        if code > (1 if action == "run" else 0):
            return f"atlas {action} failed ({code})", "not run"
    if develop_set is None:
        return "", ""
    print(f"== {model}: atlas eval --set {develop_set}", file=sys.stderr, flush=True)
    code = subprocess.run([sys.executable, "-m", "atlas.cli", "eval", str(config), "--set", str(develop_set)],
                          env=env).returncode
    return "", f"atlas eval --set failed ({code})" if code else ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Compares chat models with atlas run and atlas eval.")
    ap.add_argument("models", nargs="+", help="models, as the server names them (e.g. qwen/qwen3.6-35b-a3b)")
    ap.add_argument("--config", type=Path, default=ROOT / "atlas.openrouter.toml",
                    help="configuration that takes the model from ATLAS_MODEL (default atlas.openrouter.toml)")
    ap.add_argument("--no-run", action="store_true", help="only the table, from the reports already written")
    ap.add_argument("--out", type=Path, help="also write the table to this Markdown file")
    ap.add_argument("--develop", type=Path, help="also this set of develop cases (examples/eval/develop.yaml)")
    args = ap.parse_args()
    config = args.config.resolve()
    develop_set = args.develop.resolve() if args.develop else None
    rows, develop_rows = [], []
    for model in args.models:
        failure, develop_failure = ("", "") if args.no_run else evaluate(config, model, develop_set)
        rows.append(row(model, results(config, model), failure))
        if develop_set is not None:
            develop_rows.append(develop_row(model, develop_results(config, model, develop_set), develop_failure))
    table = "\n".join([HEADER, *rows])
    if develop_rows:
        table += "\n\n" + "\n".join([DEVELOP_HEADER, *develop_rows])
    print(f"\n{table}")
    if args.out:
        args.out.write_text(table + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
