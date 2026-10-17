"""Evaluation with questions and queries of known answer, all scored without a model.

A set is a YAML file, a list of cases of three kinds, by the key that holds the question:

    - id: trace-srs-0004                     # an agent (needs the model and the [agents] extra)
      agent: assistant                       # optional: assistant by default
      question: Which system requirement does SRS-0004 satisfy?
      expect: [SSS-0005]                     # texts the answer must contain
      tools: [trace]                         # optional: tools it must call

    - id: logs-30-days                       # the search of the index (retrieval)
      search: how long are the HMI logs kept
      expect: [SRS-0103]                     # texts that must be in the results
      kinds: [requirement]                   # optional, like document: what to search in

    - id: graph-impact-of-log                # a query of the graph, without a model
      impact: Logger.Log                     # trace, trace_code, impact, find_symbol, search_code_docs
      expect: [tracks.delete_track, SRS-0004]

An agent case passes when the answer contains every expected text (IDs, identifiers, values,
in any case) and the agent called the expected tools, without reaching max_steps; it is asked
`runs` times, since the models answer with some randomness.

A search case is ranked: each expected text is found at the position of the first result
that contains it (in its ID, document, section title or text), and the case at the position
of the last one. That gives hit@k (the share of cases whose expected texts are all in the
first k results, for k = 1, 3, 5 and 10) and the mean reciprocal rank. Every search is done
twice: hybrid (IDs, vectors and BM25, as `atlas search`) and lexical (IDs and BM25: no
request to any model), to see what the vectors add. A graph case passes when the result of
the query contains every expected text; the graph has no ranking and no randomness.

[agents.eval] sets lists the files that `atlas eval` runs (relative to atlas.toml), and
runs how many times each agent case is asked. The results go to
<workspace>/reports/eval-<set>.json.

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

__all__ = ["evaluate", "evaluate_retrieval", "evaluate_sets", "load_cases"]

GRAPH_QUERIES = ("trace", "trace_code", "impact", "find_symbol", "search_code_docs")
CASE_KEYS = {"id", "agent", "question", "expect", "tools", "search", "kinds", "document", *GRAPH_QUERIES}
KS = (1, 3, 5, 10)
SEARCH_MODES = {"hybrid": True, "lexical": False}       # name: with vectors


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
        asks = [k for k in ("question", "search", *GRAPH_QUERIES) if isinstance(c, dict) and k in c]
        if not isinstance(c, dict) or not {"id", "expect"} <= set(c) or len(asks) != 1:
            raise ConfigError(f"{where}: a case has id, expect and one of question, search or a query of "
                              f"the graph ({', '.join(GRAPH_QUERIES)})")
        unknown = sorted(set(c) - CASE_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown keys {unknown} (valid: {', '.join(sorted(CASE_KEYS))})")
        if c["id"] in seen:
            raise ConfigError(f"{where}: the id {c['id']} is repeated")
        seen.add(c["id"])
        c["expect"] = [str(x) for x in (c["expect"] if isinstance(c["expect"], list) else [c["expect"]])]
        c["kind"] = {"question": "agent", "search": "search"}.get(asks[0], "graph")
        if c["kind"] == "agent":
            c["tools"] = list(c.get("tools") or [])
            c["agent"] = c.get("agent") or "assistant"
        elif c["kind"] == "graph":
            c["query"] = asks[0]
    return cases


def evaluate(cases: list[dict], cfg: Config, runs: int = 1, context: Any = None, client: Any = None,
             on_result: Callable[[dict], None] | None = None) -> dict:
    """Asks every agent case `runs` times (the others are left out). {"results": [...],
    "passed", "total", "rate", "median_seconds", "by_case": {id: "passed/runs"}}."""
    from atlas.agents.runtime import ask, build_agent, context_for, definition
    cases = [c for c in cases if c.get("kind", "agent") == "agent"]
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
                      "calls": called, "answer": res["answer"], "tokens": res.get("tokens", {}),
                      **({"cost": res["cost"]} if "cost" in res else {}),
                      **({"error": res["error"]} if res.get("error") else {})}
            results.append(result)
            if on_result:
                on_result(result)
    passed = sum(r["passed"] for r in results)
    tokens = {k: sum(r["tokens"].get(k, 0) for r in results) for k in ("prompt", "completion")}
    return {"results": results, "passed": passed, "total": len(results), "tokens": tokens,
            **({"cost": round(sum(r.get("cost", 0) for r in results), 6)} if any("cost" in r for r in results) else {}),
            "rate": round(passed / len(results), 3) if results else 0.0,
            "median_seconds": statistics.median(r["seconds"] for r in results) if results else 0.0,
            "by_case": {c["id"]: f"{sum(r['passed'] for r in results if r['id'] == c['id'])}/{runs}" for c in cases}}


def _rank(expected: str, hits: list[dict]) -> int | None:
    """Position (from 1) of the first result that contains `expected`."""
    e = expected.lower()
    for n, h in enumerate(hits, 1):
        if any(e in str(h.get(f) or "").lower() for f in ("ref", "doc_name", "section_title", "text")):
            return n
    return None


def _summary(ranks: list[int | None]) -> dict:
    n = len(ranks)
    out = {f"hit@{k}": round(sum(1 for r in ranks if r is not None and r <= k) / n, 3) if n else 0.0 for k in KS}
    out["mrr"] = round(sum(1 / r for r in ranks if r) / n, 3) if n else 0.0
    return out


def evaluate_retrieval(cases: list[dict], cfg: Config, index: Any = None, graph: Any = None,
                       on_result: Callable[[dict], None] | None = None) -> dict:
    """Scores the search and graph cases (the agent ones are left out), without a model.
    {"search": {"cases", "hybrid": {"hit@1", ..., "mrr"}, "lexical": {...}},
    "graph": {"passed", "total", "rate"}, "results": [...]}. A search that fails (the
    embeddings server does not answer) counts as not found and keeps its error."""
    searches = [c for c in cases if c.get("kind") == "search"]
    queries = [c for c in cases if c.get("kind") == "graph"]
    results = []
    if searches:
        if index is None:
            from atlas.index import Index
            index = Index.from_config(cfg)
        for c in searches:
            result = {"id": c["id"], "kind": "search", "search": c["search"]}
            for mode, dense in SEARCH_MODES.items():
                try:
                    hits = index.search(c["search"], max(KS), c.get("document", ""), c.get("kinds"), dense=dense)
                except Exception as e:
                    hits, result[f"{mode}_error"] = [], f"{type(e).__name__}: {e}"[:300]
                ranks = {x: _rank(x, hits) for x in c["expect"]}
                result[mode] = {"rank": None if None in ranks.values() else max(ranks.values(), default=None),
                                "ranks": ranks,
                                "top": [f"{h['doc_name']} › {h.get('section_title') or ''} ({h['kind']}"
                                        + (f" {h['ref']}" if h.get("ref") else "") + ")" for h in hits[:3]]}
            results.append(result)
            if on_result:
                on_result(result)
    if queries:
        if graph is None:
            from atlas.graph import GraphTools
            graph = GraphTools.from_config(cfg)
        for c in queries:
            res = getattr(graph, c["query"])(c[c["query"]])
            text = json.dumps(res, ensure_ascii=False, default=str).lower()
            missing = [x for x in c["expect"] if x.lower() not in text]
            result = {"id": c["id"], "kind": "graph", "query": f"{c['query']} {c[c['query']]}",
                      "passed": not missing and "error" not in res, "missing": missing,
                      **({"error": res["error"]} if "error" in res else {})}
            results.append(result)
            if on_result:
                on_result(result)
    out: dict = {"results": results}
    if searches:
        found = [r for r in results if r["kind"] == "search"]
        out["search"] = {"cases": len(found), **{m: _summary([r[m]["rank"] for r in found]) for m in SEARCH_MODES}}
    if queries:
        passed = sum(r["passed"] for r in results if r["kind"] == "graph")
        out["graph"] = {"passed": passed, "total": len(queries), "rate": round(passed / len(queries), 3)}
    return out


def evaluate_sets(cfg: Config, runs: int | None = None, context: Any = None, client: Any = None,
                  on_result: Callable[[dict], None] | None = None) -> dict[str, dict]:
    """Runs the sets of [agents.eval] sets and writes reports/eval-<set>.json for each:
    {"agents": the result of evaluate, "retrieval": that of evaluate_retrieval}, each only
    if the set has cases of its kind, and "usage": what the set asked of the servers
    (requests, tokens, cost; see atlas.llm.usage)."""
    from atlas.llm import usage, usage_since
    ev = cfg["agents"].get("eval") or {}
    sets = ev.get("sets") or []
    if not sets:
        raise ConfigError("[agents.eval] sets lists no evaluation set (YAML files of cases)")
    out = {}
    for s in sets:
        path = Path(s) if Path(s).is_absolute() else cfg.path.parent / s
        cases = load_cases(path)
        res, before = {}, usage()
        if any(c["kind"] != "agent" for c in cases):
            res["retrieval"] = evaluate_retrieval(cases, cfg, on_result=on_result)
        if any(c["kind"] == "agent" for c in cases):
            res["agents"] = evaluate(cases, cfg, runs or int(ev.get("runs", 1)), context, client, on_result)
        res["usage"] = usage_since(before)          # requests, tokens and cost of the whole set
        report = cfg.workspace / "reports" / f"eval-{path.stem}.json"
        atomic_write(report, json.dumps(res, ensure_ascii=False, indent=1))
        out[path.stem] = res
    return out
