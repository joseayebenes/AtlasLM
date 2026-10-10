"""Evaluation of the agents: questions with a known answer, scored without a model. A case
passes when the answer contains every expected text (IDs, identifiers, values, in any case)
and the agent called the expected tools, without reaching max_steps.

A set is a YAML file, a list of cases:

    - id: trace-srs-0004
      agent: assistant                       # optional: assistant by default
      question: Which system requirement does SRS-0004 satisfy?
      expect: [SSS-0005]                     # texts the answer must contain
      tools: [trace]                         # optional: tools it must call

[agents.eval] sets lists the files that `atlas eval` runs (relative to atlas.toml), and
runs how many times each case is asked (the models answer with some randomness). The
results go to <workspace>/reports/eval-<set>.json.

adk eval scores the answer against a reference text (ROUGE) and the exact path of tools;
what matters here is that the IDs and values cited are the right ones, whatever the words.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any, Callable

from atlas.config import Config, ConfigError
from atlas.pipeline import atomic_write

__all__ = ["evaluate", "evaluate_sets", "load_cases"]

CASE_KEYS = {"id", "agent", "question", "expect", "tools"}


def load_cases(path: str | Path) -> list[dict]:
    import yaml
    try:
        cases = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"evaluation set {path}: {e}") from None
    if not isinstance(cases, list):
        raise ConfigError(f"evaluation set {path}: it must be a list of cases")
    seen = set()
    for i, c in enumerate(cases):
        where = f"evaluation set {path}, case {i + 1}"
        if not isinstance(c, dict) or not {"id", "question", "expect"} <= set(c):
            raise ConfigError(f"{where}: a case has id, question and expect")
        unknown = sorted(set(c) - CASE_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown keys {unknown} (valid: {', '.join(sorted(CASE_KEYS))})")
        if c["id"] in seen:
            raise ConfigError(f"{where}: the id {c['id']} is repeated")
        seen.add(c["id"])
        c["expect"] = [str(x) for x in (c["expect"] if isinstance(c["expect"], list) else [c["expect"]])]
        c["tools"] = list(c.get("tools") or [])
        c["agent"] = c.get("agent") or "assistant"
    return cases


def evaluate(cases: list[dict], cfg: Config, runs: int = 1, context: Any = None, client: Any = None,
             on_result: Callable[[dict], None] | None = None) -> dict:
    """Asks every case `runs` times. {"results": [...], "passed", "total", "rate",
    "median_seconds", "by_case": {id: "passed/runs"}}."""
    from atlas.agents.runtime import ask, build_agent, context_for, definition
    context = context or context_for(cfg)
    agents: dict[str, tuple] = {}
    results = []
    for case in cases:
        if case["agent"] not in agents:
            agents[case["agent"]] = (build_agent(case["agent"], cfg, context, client),
                                     definition(case["agent"], cfg).max_steps)
        agent, max_steps = agents[case["agent"]]
        for n in range(1, runs + 1):
            t0 = time.time()
            try:
                res = asyncio.run(ask(agent, case["question"], max_steps))
            except Exception as e:                       # a failed answer is a failed case, not the end
                res = {"answer": "", "calls": [], "stopped": False, "error": f"{type(e).__name__}: {e}"[:300]}
            called = [c["name"] for c in res["calls"]]
            missing = [x for x in case["expect"] if x.lower() not in res["answer"].lower()]
            missing_tools = [t for t in case["tools"] if t not in called]
            result = {"id": case["id"], "run": n, "seconds": round(time.time() - t0, 1),
                      "passed": not (missing or missing_tools or res["stopped"] or res.get("error")),
                      "missing": missing, "missing_tools": missing_tools, "stopped": res["stopped"],
                      "calls": called, "answer": res["answer"], **({"error": res["error"]} if res.get("error") else {})}
            results.append(result)
            if on_result:
                on_result(result)
    passed = sum(r["passed"] for r in results)
    return {"results": results, "passed": passed, "total": len(results),
            "rate": round(passed / len(results), 3) if results else 0.0,
            "median_seconds": statistics.median(r["seconds"] for r in results) if results else 0.0,
            "by_case": {c["id"]: f"{sum(r['passed'] for r in results if r['id'] == c['id'])}/{runs}" for c in cases}}


def evaluate_sets(cfg: Config, runs: int | None = None, context: Any = None, client: Any = None,
                  on_result: Callable[[dict], None] | None = None) -> dict[str, dict]:
    """Runs the sets of [agents.eval] sets and writes reports/eval-<set>.json for each."""
    ev = cfg["agents"].get("eval") or {}
    sets = ev.get("sets") or []
    if not sets:
        raise ConfigError("[agents.eval] sets lists no evaluation set (YAML files of cases)")
    out = {}
    for s in sets:
        path = Path(s) if Path(s).is_absolute() else cfg.path.parent / s
        res = evaluate(load_cases(path), cfg, runs or int(ev.get("runs", 1)), context, client, on_result)
        report = cfg.workspace / "reports" / f"eval-{path.stem}.json"
        atomic_write(report, json.dumps(res, ensure_ascii=False, indent=1))
        out[path.stem] = res
    return out
