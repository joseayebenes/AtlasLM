#!/usr/bin/env python3
"""Compares chat models on the samples: `atlas run` and `atlas eval` with each one, and a
table with how many answers of the agents each model got right, how long it took, the
tokens and the cost, and how the search ranked (hit@3 and MRR of the hybrid search).

    python tools/compare_models.py MODEL [MODEL...] [--config atlas.openrouter.toml] [--no-run] [--out FILE]

Each model goes to atlas in the ATLAS_MODEL variable. atlas.openrouter.toml gives each one its
own workspace (workspace-openrouter/<model>): a model already processed is not processed
again, and its reports stay there. ATLAS_REASONING, if set, goes to every model. --no-run
only builds the table from the reports already written. The table and every case of each
model (passed, seconds, tokens, cost) go to reports/compare-<date>.md and .json, next to
the configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.config import load_config  # noqa: E402

HEADER = ("| model | right | median (s) | total (s) | tokens/case | $ total | $ per right | hit@3 | MRR | errors "
          "| failed cases |\n|---|---|---|---|---|---|---|---|---|---|---|")


def reports(config: Path, model: str) -> list[dict]:
    """The eval reports of the model, one per set that has one."""
    os.environ["ATLAS_MODEL"] = model
    cfg = load_config(config)
    found = []
    for s in (cfg["agents"].get("eval") or {}).get("sets", []):
        report = cfg.workspace / "reports" / f"eval-{Path(s).stem}.json"
        if report.exists():
            found.append(json.loads(report.read_text(encoding="utf-8")))
    return found


def summary(model: str, found: list[dict], failure: str = "") -> dict:
    """The figures of a model: its answers (agents), their tokens and cost, and the
    search of the retrieval reports (hybrid)."""
    answers = [r for rep in found for r in (rep.get("agents") or {}).get("results", [])]
    search = next(((rep.get("retrieval") or {}).get("search") for rep in found
                   if (rep.get("retrieval") or {}).get("search")), None)
    out: dict = {"model": model, "failure": failure, "answers": len(answers)}
    if search:
        out["hit@3"], out["mrr"] = search["hybrid"]["hit@3"], search["hybrid"]["mrr"]
    if not answers:
        return out
    seconds = [r["seconds"] for r in answers]
    tokens = [sum((r.get("tokens") or {}).values()) for r in answers]
    costs = [r["cost"] for r in answers if "cost" in r]
    right = sum(r["passed"] for r in answers)
    out.update({"right": right, "median_seconds": statistics.median(seconds), "total_seconds": sum(seconds),
                "tokens_per_case": round(statistics.mean(tokens)) if any(tokens) else None,
                "cost": round(sum(costs), 6) if costs else None,
                "errors": sum(1 for r in answers if r.get("error")),
                "failed": sorted({r["id"] for r in answers if not r["passed"]}),
                "cases": [{k: r[k] for k in ("id", "run", "passed", "seconds", "tokens", "cost") if k in r}
                          for r in answers]})
    out["cost_per_right"] = round(out["cost"] / right, 6) if out["cost"] is not None and right else None
    return out


def row(s: dict) -> str:
    search = (f"{s['hit@3']:.0%} | {s['mrr']:.2f}" if "hit@3" in s else " | ")
    if s["failure"] or not s["answers"]:
        return f"| `{s['model']}` | {s['failure'] or 'no report'} | | | | | | {search} | | |"
    money = lambda v: "-" if v is None else f"{v:.4f}"
    return (f"| `{s['model']}` | {s['right']}/{s['answers']} | {s['median_seconds']:.0f} | {s['total_seconds']:.0f} "
            f"| {s['tokens_per_case'] if s['tokens_per_case'] is not None else '-'} | {money(s['cost'])} "
            f"| {money(s['cost_per_right'])} | {search} | {s['errors']} | {', '.join(s['failed']) or '-'} |")


def evaluate(config: Path, model: str) -> str:
    """atlas run and atlas eval with the model; what failed, or "" if both ran. A run with
    units that failed (exit code 1) still leaves an index to ask; a fatal error (2) does not.
    atlas eval ends with 1 when --compare finds regressions, which is not a failure here."""
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
    summaries = []
    for model in args.models:
        failure = "" if args.no_run else evaluate(config, model)
        summaries.append(summary(model, reports(config, model), failure))
    table = "\n".join([HEADER, *map(row, summaries)])
    print(f"\n{table}")
    folder = config.parent / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"compare-{date.today().isoformat()}"
    stem.with_suffix(".md").write_text(table + "\n", encoding="utf-8")
    stem.with_suffix(".json").write_text(json.dumps({"date": date.today().isoformat(), "config": str(config),
                                                     "models": summaries}, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    print(f"\nwritten: {stem.with_suffix('.md')} and .json", file=sys.stderr)
    if args.out:
        args.out.write_text(table + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
