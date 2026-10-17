"""Evaluation with questions and queries of known answer, all scored without a model.

A set is a YAML file, a list of cases of four kinds, by the key that holds the question:

    - id: trace-srs-0004                     # an agent (needs the model and the [agents] extra)
      agent: assistant                       # optional: assistant by default
      question: Which system requirement does SRS-0004 satisfy?
      expect: [SSS-0005]                     # texts the answer must contain
      tools: [trace]                         # optional: tools it must call

    - id: logs-30-days                       # the search of the index (retrieval)
      search: how long are the HMI logs kept
      expect: [SRS-0103]                     # texts that must be in the results
      kinds: [requirement]                   # optional, like document: what to search in
      tags: [paraphrase]                     # optional, in every kind: the figures by tag

    - id: graph-impact-of-log                # a query of the graph, without a model
      impact: Logger.Log                     # trace, trace_code, impact, find_symbol, search_code_docs
      expect: [tracks.delete_track, SRS-0004]

    - id: days-of-logs                       # an answer from the documents (Index.answer, the model)
      ask: How many days must the HMI keep its logs?
      expect: ["30"]
      cited: true                            # optional: each expected text in a source it cites
      document: hmi_manual.md                # optional, and scope (auto, passages, whole) and section

An agent case passes when the answer contains every expected text (IDs, identifiers, values,
in any case) and the agent called the expected tools, without reaching max_steps; it is asked
`runs` times, since the models answer with some randomness. Its result keeps the tokens and,
when the server says it, the cost.

An ask case is answered by Index.answer (`atlas ask`, the tool ask_docs) with its document,
scope and section, if it has them; it passes when the answer contains every expected text
and, with `cited: true`, each one is also in the text read from a source the answer cites
([n]): the right value from the wrong source does not pass. It keeps the requests to the
model, the scope and the seconds (and status and unsupported, when the answer has them). It
is asked `runs` times too.

A search case is ranked: each expected text is found at the position of the first result
that contains it (in its ID, document, section title or text), and the case at the position
of the last one. An expected text "section:<document>#<section id>" is found only in a
result of that section (section ids repeat in every document and are short, so they are
not looked for in the text). That gives hit@k (the share of cases whose expected texts are all in the
first k results, for k = 1, 3, 5 and 10) and the mean reciprocal rank. Every search is done
twice: hybrid (IDs, vectors and BM25, as `atlas search`) and lexical (IDs and BM25: no
request to any model), to see what the vectors add. A graph case passes when the result of
the query contains every expected text; the graph has no ranking and no randomness.

`tags` group the cases by type (paraphrase, spanish, id, code...): the report has the
figures of the search and of the graph for each tag as well as for the whole set.

[agents.eval] sets lists the files that `atlas eval` runs (relative to atlas.toml), and
runs how many times each agent and ask case is asked. The header `meta` of a report says
what was measured: the model, its reasoning_effort and provider (extra_body), the embeddings
model, the version of atlas, the date, runs and the hash of the YAML. The results go to
<workspace>/reports/eval-<set>.json, overwritten each time, and to
eval-<set>-<version>-<date>.json, which stays: `atlas eval --compare` compares each case with
the last report of another version of atlas (compare_reports).

`atlas eval --generate FILE --n 50` writes a set of retrieval and graph cases taken from the
index and the graph of the workspace (generate_cases), the same each time for the same data:
the regression check of a project nobody has written cases for yet.

adk eval scores the answer against a reference text (ROUGE) and the exact path of tools;
what matters here is that the IDs and values cited are the right ones, whatever the words.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from atlas.config import SEARCH_SCOPES, Config, ConfigError
from atlas.pipeline import atomic_write

__all__ = ["compare_reports", "evaluate", "evaluate_answers", "evaluate_retrieval", "evaluate_sets", "generate_cases",
           "load_cases", "previous_report", "write_cases"]

GRAPH_QUERIES = ("trace", "trace_code", "impact", "find_symbol", "search_code_docs")
CASE_KEYS = {"id", "agent", "question", "expect", "tools", "search", "kinds", "document", "tags", "ask", "scope",
             "section", "cited", *GRAPH_QUERIES}
KS = (1, 3, 5, 10)
SEARCH_MODES = {"hybrid": True, "lexical": False}       # name: with vectors
SECTION = "section:"                                    # expected text: section:<document>#<section id>


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
        asks = [k for k in ("question", "search", "ask", *GRAPH_QUERIES) if isinstance(c, dict) and k in c]
        if not isinstance(c, dict) or not {"id", "expect"} <= set(c) or len(asks) != 1:
            raise ConfigError(f"{where}: a case has id, expect and one of question, search, ask or a query of "
                              f"the graph ({', '.join(GRAPH_QUERIES)})")
        unknown = sorted(set(c) - CASE_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown keys {unknown} (valid: {', '.join(sorted(CASE_KEYS))})")
        if c["id"] in seen:
            raise ConfigError(f"{where}: the id {c['id']} is repeated")
        seen.add(c["id"])
        c["expect"] = [str(x) for x in (c["expect"] if isinstance(c["expect"], list) else [c["expect"]])]
        tags = c.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            raise ConfigError(f"{where}: tags is a list of words, e.g. [paraphrase, spanish]")
        c["tags"] = list(dict.fromkeys(t.strip() for t in tags))
        c["kind"] = {"question": "agent", "search": "search", "ask": "ask"}.get(asks[0], "graph")
        if c["kind"] == "ask":
            if c.get("scope", "auto") not in SEARCH_SCOPES:
                raise ConfigError(f"{where}: scope is one of {', '.join(SEARCH_SCOPES)}")
            if not isinstance(c.get("cited", False), bool):
                raise ConfigError(f"{where}: cited is true or false")
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
            tokens = res.get("tokens") or {"prompt": 0, "completion": 0}
            called = [c["name"] for c in res["calls"]]
            missing = [x for x in case["expect"] if x.lower() not in res["answer"].lower()]
            missing_tools = [t for t in case["tools"] if t not in called]
            result = {"id": case["id"], "run": n, "seconds": round(time.time() - t0, 1),
                      "passed": not (missing or missing_tools or res["stopped"] or res.get("error")),
                      "missing": missing, "missing_tools": missing_tools, "stopped": res["stopped"],
                      "calls": called, "answer": res["answer"], "tags": case.get("tags", []), "tokens": tokens,
                      **({"cost": res["cost"]} if res.get("cost") is not None else {}),
                      **({"error": res["error"]} if res.get("error") else {})}
            results.append(result)
            if on_result:
                on_result(result)
    out = _answer_figures(results, cases, runs)
    out["tokens"] = {k: sum(r["tokens"].get(k, 0) for r in results) for k in ("prompt", "completion")}
    costs = [r["cost"] for r in results if "cost" in r]
    if costs:
        out["cost"] = round(sum(costs), 6)
    return out


def _answer_figures(results: list[dict], cases: list[dict], runs: int) -> dict:
    """{"results", "passed", "total", "rate", "median_seconds", "by_case", "tags"} of the
    answers of agent or ask cases, each asked `runs` times."""
    passed = sum(r["passed"] for r in results)
    out = {"results": results, "passed": passed, "total": len(results),
           "rate": round(passed / len(results), 3) if results else 0.0,
           "median_seconds": statistics.median(r["seconds"] for r in results) if results else 0.0,
           "by_case": {c["id"]: f"{sum(r['passed'] for r in results if r['id'] == c['id'])}/{runs}" for c in cases}}
    tags = list(dict.fromkeys(t for r in results for t in r.get("tags", [])))
    if tags:
        out["tags"] = {t: _graph_figures([r for r in results if t in r.get("tags", [])]) for t in tags}
    return out


CITED = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")       # [2], [1][3], [1, 3]


def evaluate_answers(cases: list[dict], cfg: Config, runs: int = 1, index: Any = None,
                     on_result: Callable[[dict], None] | None = None) -> dict:
    """Asks Index.answer every ask case `runs` times (the others are left out), with its
    document, scope and section if it has them. {"results": [...], "passed", "total",
    "rate", "median_seconds", "requests", "by_case"}. A case passes when the answer has
    every expected text and, with cited, each is in the context of a source it cites."""
    cases = [c for c in cases if c.get("kind") == "ask"]
    if index is None:
        from atlas.index import Index
        index = Index.from_config(cfg)
    accepts = set(inspect.signature(index.answer).parameters)
    results = []
    for case in cases:
        options = {k: case[k] for k in ("document", "scope", "section") if case.get(k)}
        unsupported = sorted(set(options) - accepts)
        for n in range(1, runs + 1):
            t0 = time.time()
            try:
                if unsupported:
                    raise ValueError(f"Index.answer of this atlas takes no {', '.join(unsupported)}")
                res = index.answer(case["ask"], **options)
            except Exception as e:                       # a failed answer is a failed case, not the end
                res = {"answer": "", "sources": [], "error": f"{type(e).__name__}: {e}"[:300]}
            text = res.get("answer", "")
            numbers = {int(x) for group in CITED.findall(text) for x in re.split(r"\s*,\s*", group)}
            cited = [s for s in res.get("sources", []) if s.get("n") in numbers]
            read = " ".join(str(s.get("context") or s.get("excerpt") or "") for s in cited).lower()
            missing = [x for x in case["expect"] if x.lower() not in text.lower()]
            uncited = [x for x in case["expect"] if x.lower() not in read] if case.get("cited") else []
            result = {"id": case["id"], "kind": "ask", "run": n, "seconds": round(time.time() - t0, 1),
                      "passed": not (missing or uncited or res.get("error")), "missing": missing, "uncited": uncited,
                      "scope": res.get("scope", ""), "requests": res.get("requests", 0),
                      "documents": res.get("documents", []), "cited": [s.get("cite", "") for s in cited],
                      "answer": text, "tags": case.get("tags", []),
                      **{k: res[k] for k in ("status", "unsupported") if k in res},
                      **({"error": res["error"]} if res.get("error") else {})}
            results.append(result)
            if on_result:
                on_result(result)
    out = _answer_figures(results, cases, runs)
    out["requests"] = sum(r["requests"] for r in results)
    return out


def _rank(expected: str, hits: list[dict]) -> int | None:
    """Position (from 1) of the first result that contains `expected`, or that is in the
    section of "section:<document>#<section id>"."""
    e = expected.lower()
    for n, h in enumerate(hits, 1):
        if e.startswith(SECTION):
            if f"{h.get('doc_name') or ''}#{h.get('section_id') or ''}".lower() == e[len(SECTION):]:
                return n
        elif any(e in str(h.get(f) or "").lower() for f in ("ref", "doc_name", "section_title", "text")):
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
    results: list[dict] = []
    if searches:
        if index is None:
            from atlas.index import Index
            index = Index.from_config(cfg)
        for c in searches:
            result = {"id": c["id"], "kind": "search", "search": c["search"], "tags": c.get("tags", [])}
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
            result = {"id": c["id"], "kind": "graph", "query": f"{c['query']} {c[c['query']]}", "tags": c.get("tags", []),
                      "passed": not missing and "error" not in res, "missing": missing,
                      **({"error": res["error"]} if "error" in res else {})}
            results.append(result)
            if on_result:
                on_result(result)
    out: dict = {"results": results}
    for kind, figures in (("search", _search_figures), ("graph", _graph_figures)):
        found = [r for r in results if r["kind"] == kind]
        if found:
            out[kind] = figures(found)
            tags = list(dict.fromkeys(t for r in found for t in r["tags"]))      # in their order
            if tags:
                out[kind]["tags"] = {t: figures([r for r in found if t in r["tags"]]) for t in tags}
    return out


def _search_figures(found: list[dict]) -> dict:
    return {"cases": len(found), **{m: _summary([r[m]["rank"] for r in found]) for m in SEARCH_MODES}}


def _graph_figures(found: list[dict]) -> dict:
    passed = sum(r["passed"] for r in found)
    return {"passed": passed, "total": len(found), "rate": round(passed / len(found), 3)}


def evaluate_sets(cfg: Config, runs: int | None = None, context: Any = None, client: Any = None,
                  on_result: Callable[[dict], None] | None = None) -> dict[str, dict]:
    """Runs the sets of [agents.eval] sets and writes, for each, reports/eval-<set>.json
    (overwritten) and reports/eval-<set>-<version>-<date>.json (kept, for compare_reports):
    {"meta": see meta, "retrieval": the result of evaluate_retrieval, "answers": that of
    evaluate_answers, "agents": that of evaluate}, each only if the set has cases of its kind."""
    from atlas import __version__
    ev = cfg["agents"].get("eval") or {}
    sets = ev.get("sets") or []
    if not sets:
        raise ConfigError("[agents.eval] sets lists no evaluation set (YAML files of cases)")
    runs = runs or int(ev.get("runs", 1))
    index = None
    out = {}
    for s in sets:
        path = Path(s) if Path(s).is_absolute() else cfg.path.parent / s
        cases = load_cases(path)
        started = datetime.now()
        res: dict = {"meta": meta(cfg, path, runs, started)}
        kinds = {c["kind"] for c in cases}
        if kinds & {"search", "ask"} and index is None:
            from atlas.index import Index
            index = Index.from_config(cfg)
        if kinds & {"search", "graph"}:
            res["retrieval"] = evaluate_retrieval(cases, cfg, index, on_result=on_result)
        if "ask" in kinds:
            res["answers"] = evaluate_answers(cases, cfg, runs, index, on_result)
        if "agent" in kinds:
            res["agents"] = evaluate(cases, cfg, runs, context, client, on_result)
        text = json.dumps(res, ensure_ascii=False, indent=1)
        reports = cfg.workspace / "reports"
        atomic_write(reports / f"eval-{path.stem}.json", text)
        atomic_write(reports / f"eval-{path.stem}-{__version__}-{started.date().isoformat()}.json", text)
        out[path.stem] = res
    return out


def meta(cfg: Config, path: Path, runs: int, started: datetime) -> dict:
    """What a report measured: the set, the version of atlas, the date, the chat model of
    the agents and of [llm], its reasoning_effort and provider ([llm.extra_body]), the
    embeddings model, runs and the hash of the YAML (another set under the same name is
    another figure)."""
    from atlas import __version__
    llm, agents = cfg.get("llm") or {}, cfg.get("agents") or {}
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        digest = ""
    return {"set": path.stem, "version": __version__, "date": started.isoformat(timespec="seconds"),
            "model": agents.get("model") or llm.get("model", ""), "llm_model": llm.get("model", ""),
            "reasoning_effort": llm.get("reasoning_effort", ""),
            "provider": (llm.get("extra_body") or {}).get("provider"),
            "embeddings_model": (cfg.get("embeddings") or {}).get("model", ""), "runs": runs, "yaml_sha256": digest}


def previous_report(cfg: Config, name: str, version: str) -> dict | None:
    """The last report kept of the set `name` (reports/eval-<set>-<version>-<date>.json)
    written by a version of atlas other than `version`, or None."""
    found = []
    for path in (cfg.workspace / "reports").glob(f"eval-{name}-*.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta = report.get("meta") if isinstance(report, dict) else None
        if isinstance(meta, dict) and meta.get("set") == name and meta.get("version") not in (None, version):
            found.append((str(meta.get("date", "")), report))
    return max(found, key=lambda x: x[0])[1] if found else None


def _cases_of(report: dict) -> dict[str, dict]:
    """What each case of a report got: {"kind", "hybrid", "lexical"} for a search (its
    ranks), {"kind", "passed"} for a query of the graph, {"kind", "passed", "runs"} for a
    question (how many of its runs passed)."""
    cases: dict[str, dict] = {}
    for r in (report.get("retrieval") or {}).get("results", []):
        if r.get("kind") == "search":
            cases[r["id"]] = {"kind": "search", **{m: (r.get(m) or {}).get("rank") for m in SEARCH_MODES}}
        else:
            cases[r["id"]] = {"kind": r.get("kind", "graph"), "passed": bool(r.get("passed"))}
    for section, kind in (("answers", "ask"), ("agents", "agent")):
        for r in (report.get(section) or {}).get("results", []):
            c = cases.setdefault(r["id"], {"kind": kind, "passed": 0, "runs": 0})
            c["passed"] += bool(r.get("passed"))
            c["runs"] += 1
    return cases


def _lower(before: int | None, after: int | None) -> bool:
    """Whether a search ranked lower: found before and now further down, or not found."""
    return before is not None and (after is None or after > before)


def compare_reports(old: dict, new: dict) -> dict:
    """Each case of `new` against the same case of `old`: {"against": the meta of old,
    "cases": [{"id", "kind", "before", "after", "change"}], "regressions": [ids],
    "removed": [ids only in old]}. change is "regression" (a search ranked lower or not
    found in either mode; a query or a question that passes less often), "better", "same"
    or "new" (a case old did not have)."""
    before, after = _cases_of(old), _cases_of(new)
    rate = lambda c: c["passed"] / c["runs"] if "runs" in c else float(c["passed"])
    cases = []
    for cid, now in after.items():
        was = before.get(cid)
        if was is None or was["kind"] != now["kind"]:
            change = "new"
        elif now["kind"] == "search":
            change = "regression" if any(_lower(was[m], now[m]) for m in SEARCH_MODES) else \
                "better" if any(_lower(now[m], was[m]) for m in SEARCH_MODES) else "same"
        else:
            change = "regression" if rate(now) < rate(was) else "better" if rate(now) > rate(was) else "same"
        cases.append({"id": cid, "kind": now["kind"], "before": was, "after": now, "change": change})
    return {"against": old.get("meta", {}), "cases": cases,
            "regressions": [c["id"] for c in cases if c["change"] == "regression"],
            "removed": [cid for cid in before if cid not in after]}


GENERATED = "# Generated by atlas eval --generate"     # first line of a generated set
GENERATE_KINDS = ("id", "section", "trace", "impact")
MAX_EXPECTED = 10                                     # code, tests or callers expected in one case


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def generate_cases(cfg: Config, n: int = 50, seed: int = 0) -> list[dict]:
    """Up to n cases of known answer from the index and the graph of the workspace, without
    a model and the same each time for the same data (requirements, sections and
    subprograms are drawn with a fixed seed): a search with the exact ID of a requirement,
    expecting it; a search with the title of a section (one no other section has),
    expecting that section; the trace of a requirement that has code, expecting its code
    and its tests; and the impact of a subprogram that has callers, expecting them. The four
    kinds take turns until there are n. Only retrieval and graph cases: no agent."""
    import random
    from atlas.graph import KnowledgeGraph
    from atlas.graph.knowledge import REQUIREMENT_KINDS
    from atlas.index import IndexStore
    db = cfg.workspace / "atlas.db"
    if not db.exists():
        raise ConfigError(f"nothing to generate cases from: {db} does not exist (atlas run first)")
    store, graph = IndexStore(db), KnowledgeGraph.open(db)
    G = graph.G

    def called(node: str, edge: str) -> bool:
        return any(e.get("kind") == edge for _, _, e in G.in_edges(node, data=True))

    sections = store.sections()
    titles = Counter(title.strip().lower() for _, _, title in sections)
    candidates = {
        "id": store.refs("requirement"),
        "section": [s for s in sections if titles[s[2].strip().lower()] == 1 and re.search(r"[^\W\d_]{3}", s[2])],
        "trace": sorted(r for r, d in G.nodes(data=True) if d.get("kind") in REQUIREMENT_KINDS and called(r, "implements")),
        "impact": sorted(s for s, d in G.nodes(data=True) if d.get("kind") == "subprogram" and called(s, "calls")),
    }
    rng = random.Random(seed)
    for found in candidates.values():
        rng.shuffle(found)
    chosen: dict[str, list] = {k: [] for k in GENERATE_KINDS}
    while sum(map(len, chosen.values())) < n and any(candidates.values()):
        for k in GENERATE_KINDS:
            if candidates[k] and sum(map(len, chosen.values())) < n:
                chosen[k].append(candidates[k].pop())

    cases = [{"id": f"gen-id-{_slug(r)}", "search": r, "expect": [r], "tags": ["generated", "id"]}
             for r in sorted(chosen["id"])]
    cases += [{"id": f"gen-section-{_slug(doc)}-{_slug(sid)}", "search": title.strip(),
               "expect": [f"{SECTION}{doc}#{sid}"], "tags": ["generated", "section"]}
              for doc, sid, title in sorted(chosen["section"])]
    for r in sorted(chosen["trace"]):
        res = graph.trace(r)
        expect = [r] + [i["id"] for i in res.get("implemented_by", {}).get("items", [])][:MAX_EXPECTED] \
            + [t["id"] for t in res.get("verified_by", [])][:MAX_EXPECTED]
        if "error" not in res:
            cases.append({"id": f"gen-trace-{_slug(r)}", "trace": r, "expect": expect, "tags": ["generated", "trace"]})
    for s in sorted(chosen["impact"]):
        callers = sorted(i["id"] for i in graph.callers(s, limit=len(G)).get("items", []))[:MAX_EXPECTED]
        cases.append({"id": f"gen-impact-{_slug(s)}", "impact": s, "expect": callers, "tags": ["generated", "impact"]})
    return cases


def write_cases(path: str | Path, cases: list[dict], cfg: Config, seed: int = 0) -> Path:
    """Writes a generated set. A file that exists and was not generated (a set written by
    hand) is not overwritten."""
    import yaml
    path = Path(path)
    if path.exists() and not path.read_text(encoding="utf-8").startswith(GENERATED):
        raise ConfigError(f"{path} exists and was not generated by atlas eval --generate: it is not overwritten")
    kinds = Counter(c["tags"][1] for c in cases)
    header = (f"{GENERATED} from {cfg.path.name} on {datetime.now().date().isoformat()} (seed {seed}): "
              f"{len(cases)} cases\n# ({', '.join(f'{kinds[k]} {k}' for k in GENERATE_KINDS)}). It is written again as a "
              "whole: cases written by hand go in another set.\n")
    atomic_write(path, header + yaml.safe_dump(cases, allow_unicode=True, sort_keys=False, width=120, default_flow_style=None))
    return path
