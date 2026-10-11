"""Compliance of the code with a requirement: whether the code that implements it does all
that it asks, obligation by obligation, judged by the agent compliance_checker. A help for
the inspection of the code, not a proof: what reading code cannot decide (timing,
performance, code that is not shown) is reported as such, and the tests are what verify.

Two steps, as the verification against the documentation:
1. Without a model, the evidence: the official text of the requirement (or what the
   document that states it says, if it is not in DOORS), the code that implements it
   according to the graph (`Req:` tags) with its comment and source, what that code calls
   (with the comments of the callees) and the tests that verify or cover it.
2. With the model, the judgement: the requirement split into its obligations, each met,
   not_met, partial or cannot_tell, with the code that decides it. The verdict follows from
   them, without the model: does_not_comply if one is not met or only in part, cannot_tell
   if none is unmet but one cannot be decided by reading, complies if all are met.

A requirement without code is "no_code", without asking the model. The judgements are kept
in <workspace>/reports/.comply_cache.json by the hash of the evidence, the instructions and
the model.

    res = comply("SRS-0004", cfg)          # {"id", "verdict", "obligations", "code", ...}
    report = comply_all("SRS_TMS", cfg)     # every requirement of a module that has code
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from atlas.agents.definitions import AgentDef
from atlas.config import Config
from atlas.pipeline import atomic_write, sha256_text

__all__ = ["STATUSES", "VERDICTS", "comply", "comply_all", "evidence"]

log = logging.getLogger(__name__)

STATUSES = ("met", "not_met", "partial", "cannot_tell")
VERDICTS = ("complies", "does_not_comply", "cannot_tell", "no_code")
MAX_CODE_CHARS = 2500          # of each code element
MAX_ELEMENTS = 6
SCHEMA = {"type": "object", "required": ["obligations", "summary"], "properties": {
    "obligations": {"type": "array", "items": {"type": "object", "required": ["text", "status", "evidence"],
                                               "properties": {"text": {"type": "string"},
                                                              "status": {"type": "string", "enum": list(STATUSES)},
                                                              "evidence": {"type": "string"}}}},
    "summary": {"type": "string"}}}


def evidence(req_id: str, graph) -> dict:
    """What the model gets, without a model: {"id", "text", "source", "code": [{"id", "kind",
    "where", "doc", "source", "calls"}], "tests"} or {"error"}."""
    t = graph.trace(req_id)
    if "error" in t:
        return t
    text, source = t.get("text", ""), "DOORS"
    if not text:                                   # not in DOORS: the document that states it
        stated = next((x for x in t["documents"] if x.get("text")), None)
        text, source = (stated["text"], f"{stated['document']} › {stated['title']}") if stated else ("", "")
    code = []
    for n in graph._code_of(t["id"])[:MAX_ELEMENTS]:
        d = graph.G.nodes[n]
        src = graph.read_source(n)
        listing = src.get("source", "") if "error" not in src else ""
        calls = [{"id": c, "doc": graph.G.nodes[c].get("doc", "")}
                 for c in graph._linked(n, {"calls"}, incoming=False)]
        code.append({"id": n, "kind": d.get("kind"), "doc": d.get("doc", ""),
                     "where": f"{d.get('file', '')}:{d.get('start_line') or d.get('line', '')}",
                     "source": listing[:MAX_CODE_CHARS] + ("\n…" if len(listing) > MAX_CODE_CHARS else ""),
                     "calls": calls})
    tests = sorted({x["id"] for x in t.get("verified_by", [])})
    return {"id": t["id"], "text": text, "source": source, "code": code, "tests": tests}


def _prompt(ev: dict) -> str:
    lines = [f"Requirement {ev['id']} ({ev['source']}): {ev['text'] or '(no text)'}", "", "Code that implements it:"]
    for c in ev["code"]:
        lines += ["", f"[{c['id']}] ({c['kind']}, {c['where']})", f"Comment: {c['doc'] or '(none)'}",
                  c["source"] or "(source not available)"]
        if c["calls"]:
            lines.append("It calls: " + "; ".join(f"{x['id']}" + (f" ({x['doc']})" if x["doc"] else "")
                                                  for x in c["calls"]))
    lines += ["", "Tests that verify or cover it: " + (", ".join(ev["tests"]) or "none")]
    return "\n".join(lines)


def _clean(answer: dict) -> dict:
    """The obligations with the shape of SCHEMA, and the verdict that follows from them."""
    obligations = []
    for o in answer.get("obligations") or []:
        if isinstance(o, dict) and str(o.get("text") or "").strip():
            obligations.append({"text": str(o["text"]).strip(),
                                "status": o.get("status") if o.get("status") in STATUSES else "cannot_tell",
                                "evidence": str(o.get("evidence") or "")})
    statuses = {o["status"] for o in obligations}
    verdict = ("does_not_comply" if statuses & {"not_met", "partial"} else
               "cannot_tell" if "cannot_tell" in statuses or not obligations else "complies")
    return {"verdict": verdict, "obligations": obligations, "summary": str(answer.get("summary") or "")}


class Checker:
    """Judges requirements against their code with the agent compliance_checker, with the
    cache of judgements."""

    def __init__(self, cfg: Config, llm: Any = None, graph=None):
        from atlas.agents.runtime import agent_service, definition
        from atlas.graph import KnowledgeGraph
        self.cfg = cfg
        self.agent: AgentDef = definition("compliance_checker", cfg)
        self.service = agent_service(self.agent, cfg)
        self._llm = llm
        self.graph = graph or KnowledgeGraph.from_config(cfg)
        self.cache_path = cfg.workspace / "reports" / ".comply_cache.json"
        try:
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        self.asked = 0
        self._lock = threading.Lock()

    @property
    def llm(self):
        if self._llm is None:
            from atlas.llm import JsonLlm
            self._llm = JsonLlm(self.service)
        return self._llm

    def check(self, req_id: str) -> dict:
        ev = evidence(req_id, self.graph)
        if "error" in ev:
            return {"id": req_id, **ev}
        base = {"id": ev["id"], "text": ev["text"], "code": [c["id"] for c in ev["code"]], "tests": ev["tests"]}
        if not ev["code"]:
            return {**base, "verdict": "no_code", "obligations": [],
                    "summary": "No code cites this requirement: there is nothing to check."}
        prompt = _prompt(ev)
        key = sha256_text(prompt, self.agent.instruction, self.service.model)[:24]
        with self._lock:
            judged = self.cache.get(key)
        if judged is None:
            judged = _clean(self.llm.ask_json(self.agent.instruction, prompt, SCHEMA, "compliance"))
            with self._lock:
                self.cache[key] = judged
                self.asked += 1
        return {**base, **judged}

    def save_cache(self) -> None:
        atomic_write(self.cache_path, json.dumps(self.cache, ensure_ascii=False, indent=1))


def comply(req_id: str, cfg: Config, llm: Any = None, graph=None) -> dict:
    """The compliance of the code with one requirement (see the module docstring)."""
    checker = Checker(cfg, llm, graph)
    res = checker.check(req_id)
    checker.save_cache()
    return res


def _report(target: str, results: list[dict]) -> str:
    count = {v: sum(r.get("verdict") == v for r in results) for v in VERDICTS}
    md = [f"# Compliance of the code: {target}", "",
          f"{len(results)} requirements: {count['complies']} comply, {count['does_not_comply']} do not, "
          f"{count['cannot_tell']} cannot be decided by reading the code, {count['no_code']} without code.", "",
          "A judgement of the code by a model, obligation by obligation: a help for the inspection, not a "
          "proof. Timing, performance and behaviour of code that is not shown are left to the tests.", ""]
    mark = {"met": "✓", "not_met": "✗", "partial": "◐", "cannot_tell": "?"}
    for r in results:
        if r.get("verdict") in ("no_code", None):
            continue
        md += [f"## {r['id']}: {r['verdict']}", "", r.get("text", ""), "",
               f"Code: {', '.join(r['code'])}. Tests: {', '.join(r['tests']) or 'none'}.", "", r.get("summary", ""), ""]
        md += [f"- {mark[o['status']]} {o['text']}: {o['evidence']}" for o in r["obligations"]]
        md.append("")
    md += ["## Without code", "", ", ".join(r["id"] for r in results if r.get("verdict") == "no_code") or "-"]
    return "\n".join(md) + "\n"


def comply_all(target: str, cfg: Config, llm: Any = None, graph=None, workers: int = 0) -> dict:
    """Checks a requirement ID, every requirement of a DOORS module, or all of them ("*"),
    in parallel ([pipeline] workers), and writes <workspace>/reports/comply-<target>.json
    and .md. {"target", "results", "counts", "asked", "files"} or {"error"}."""
    checker = Checker(cfg, llm, graph)
    g = checker.graph
    if target != "*" and g._requirement(target)[0]:
        ids = [g._requirement(target)[0]]
    else:
        ids, err = g._module_requirements("" if target == "*" else target)
        if err:
            return {"error": f"'{target}' is neither a requirement nor a module", **{k: x for k, x in err.items()
                                                                                    if k == "modules"}}
        ids = [r for r in ids if g.G.nodes[r].get("module")]
    with ThreadPoolExecutor(max(1, workers or cfg["pipeline"]["workers"])) as pool:
        results = list(pool.map(checker.check, ids))
    checker.save_cache()
    folder = cfg.workspace / "reports"
    name = "all" if target == "*" else "".join(c if c.isalnum() or c in "-_" else "_" for c in target)
    files = [folder / f"comply-{name}.json", folder / f"comply-{name}.md"]
    atomic_write(files[0], json.dumps(results, ensure_ascii=False, indent=1))
    atomic_write(files[1], _report(target, results))
    return {"target": target, "results": results,
            "counts": {v: sum(r.get("verdict") == v for r in results) for v in VERDICTS},
            "asked": checker.asked, "files": [str(f) for f in files]}
