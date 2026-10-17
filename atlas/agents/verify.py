"""Verification of requirements against the documentation: for each requirement that has
documentation, whether its official text (DOORS) and the documents say the same, and the
inconsistencies (values, conditions, contradictions, scope, what is left open).

Two steps:
1. Without a model, the evidence: the official text and attributes, the requirements it is
   linked to, the passages of the documents that state it or talk about it (the sections of
   the graph and, if there is an index, what it finds for the ID and the text) and the
   values that differ, in value or in unit (atlas.graph.values).
2. With the model, the judgement: the agent requirement_verifier (its Markdown file gives
   the instructions, the model and the reasoning) answers a JSON with a verdict, the issues
   with the words of each text and their source, and a summary.

A requirement without any passage is "no_documentation", without asking the model. The
judgements are kept in <workspace>/reports/.verify_cache.json by the hash of the evidence,
the instructions and the model: verifying again only asks for what changed.

    res = verify("SRS-0001", cfg)            # {"id", "verdict", "issues", "summary", ...}
    report = verify_all("SRS_TMS", cfg)       # every requirement of a module, written to reports/
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

__all__ = ["VERDICTS", "evidence", "verify", "verify_all"]

log = logging.getLogger(__name__)

VERDICTS = ("consistent", "inconsistent", "no_documentation")
ISSUE_TYPES = ("value", "condition", "contradiction", "scope", "undefined")
SCHEMA = {"type": "object", "required": ["verdict", "issues", "summary"], "properties": {
    "verdict": {"type": "string", "enum": list(VERDICTS)},
    "issues": {"type": "array", "items": {"type": "object", "required": ["type", "official", "document", "source"],
                                          "properties": {"type": {"type": "string", "enum": list(ISSUE_TYPES)},
                                                         "official": {"type": "string"},
                                                         "document": {"type": "string"},
                                                         "source": {"type": "string"},
                                                         "explanation": {"type": "string"}}}},
    "summary": {"type": "string"}}}
MAX_PASSAGES = 8
MAX_PASSAGE_CHARS = 1500


def evidence(req_id: str, graph, index=None) -> dict:
    """What the model gets to judge a requirement, without a model: {"id", "official",
    "linked", "passages": [{"source", "text"}], "values_differ"} or {"error"}. values_differ:
    the sections whose quantities differ from DOORS in value or in unit ({"source", "doors",
    "document", "kind"}, atlas.graph.values)."""
    t = graph.trace(req_id)
    if "error" in t:
        return t
    linked = [{"id": x["id"], "type": x["type"], "text": graph.G.nodes[x["id"]].get("text", "")}
              for x in t["links_out"] + t["links_in"]]
    passages, seen = [], set()

    def add(source: str, text: str) -> None:
        key = " ".join(text.split())[:200]
        if text and key not in seen and len(passages) < MAX_PASSAGES:
            seen.add(key)
            passages.append({"source": source, "text": text[:MAX_PASSAGE_CHARS]})

    if index is not None:                     # the statements and the passages around them
        from atlas.index import DOORS_PREFIX, cite
        hits = index.requirement(t["id"])
        try:
            hits += index.search(f"{t['id']} {t.get('text', '')}", k=6)
        except Exception as e:                # no embeddings server: the statements are enough
            log.warning("verify %s: no search in the documents (%s)", t["id"], e)
        for h in hits:
            if not h["doc"].startswith(DOORS_PREFIX) and (h["kind"] == "requirement" or t["id"] in h["text"]):
                add(cite(h), h["text"])
    for x in t["documents"]:                  # the sections of the graph, if the index had nothing
        add(f"{x['document']} › {x['title']}", x.get("text", ""))
    from atlas.graph.values import values_differ
    differ = []                               # values and units apart: a format apart says nothing
    for x in t["documents"]:
        d = values_differ(t.get("text", ""), x.get("text", "")) if t.get("in_doors", True) else None
        if d and d["kind"] != "format_only":
            differ.append({"source": f"{x['document']} › {x['title']}", **d})
    return {"id": t["id"], "in_doors": t.get("in_doors", True),
            "official": {k: t[k] for k in ("module", "text", "attributes", "parent") if t.get(k)},
            "linked": linked, "passages": passages, "values_differ": differ}


def _prompt(ev: dict) -> str:
    o = ev["official"]
    lines = [f"Requirement {ev['id']}" + (f" (module {o['module']})" if o.get("module") else ""),
             f"Official text (DOORS): {o.get('text') or '(not in DOORS)'}"]
    if o.get("attributes"):
        attributes = {k: v for k, v in o["attributes"].items() if v not in ("", None)}
        lines.append("Attributes: " + ", ".join(f"{k} = {v}" for k, v in attributes.items()))
    for x in ev["linked"]:
        lines.append(f"Linked: {x['type']} {x['id']}" + (f": {x['text']}" if x["text"] else ""))
    lines.append("\nPassages of the documents:")
    lines += [f"[{i}] {p['source']}\n{p['text']}" for i, p in enumerate(ev["passages"], 1)]
    if ev["values_differ"]:
        lines.append("\nValues found only in one of the texts (a comparison without a model):")
        lines += [f"- {d['source']}: DOORS {', '.join(d['doors']) or '-'}; document {', '.join(d['document']) or '-'}"
                  + (" (other units: compare them)" if d["kind"] == "unit_only" else "") for d in ev["values_differ"]]
    return "\n".join(lines)


def _clean(answer: dict) -> dict:
    """The answer of the model with the shape of SCHEMA, whatever a small model wrote."""
    verdict = answer.get("verdict") if answer.get("verdict") in VERDICTS else "inconsistent"
    issues = []
    for i in answer.get("issues") or []:
        if isinstance(i, dict):
            issues.append({"type": i.get("type") if i.get("type") in ISSUE_TYPES else "contradiction",
                           **{k: str(i.get(k) or "") for k in ("official", "document", "source", "explanation")}})
    if verdict == "consistent" and issues:
        verdict = "inconsistent"
    return {"verdict": verdict, "issues": issues, "summary": str(answer.get("summary") or "")}


class Verifier:
    """Judges requirements with the agent requirement_verifier, with the cache of judgements."""

    def __init__(self, cfg: Config, llm: Any = None, graph=None, index=None):
        from atlas.agents.runtime import agent_service, definition
        from atlas.graph import KnowledgeGraph
        self.cfg = cfg
        self.agent: AgentDef = definition("requirement_verifier", cfg)
        self.service = agent_service(self.agent, cfg)
        self._llm = llm
        self.graph = graph or KnowledgeGraph.from_config(cfg)
        self.index = index
        self.cache_path = cfg.workspace / "reports" / ".verify_cache.json"
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

    def verify(self, req_id: str) -> dict:
        ev = evidence(req_id, self.graph, self.index)
        if "error" in ev:
            return {"id": req_id, **ev}
        base = {"id": ev["id"], "official": ev["official"].get("text", ""),
                "sources": [p["source"] for p in ev["passages"]], "values_differ": ev["values_differ"]}
        if not ev["passages"]:
            return {**base, "verdict": "no_documentation", "issues": [],
                    "summary": "No document states or describes this requirement."}
        prompt = _prompt(ev)
        key = sha256_text(prompt, self.agent.instruction, self.service.model)[:24]
        with self._lock:
            cached = self.cache.get(key)
        if cached is None:
            cached = _clean(self.llm.ask_json(self.agent.instruction, prompt, SCHEMA, "verification"))
            with self._lock:
                self.cache[key] = cached
                self.asked += 1
        return {**base, **cached}

    def save_cache(self) -> None:
        atomic_write(self.cache_path, json.dumps(self.cache, ensure_ascii=False, indent=1))


def verify(req_id: str, cfg: Config, llm: Any = None, graph=None, index=None) -> dict:
    """The verification of one requirement (see the module docstring)."""
    v = Verifier(cfg, llm, graph, index)
    res = v.verify(req_id)
    v.save_cache()
    return res


def _report(target: str, results: list[dict]) -> str:
    count = {v: sum(r.get("verdict") == v for r in results) for v in VERDICTS}
    md = [f"# Verification of requirements: {target}", "",
          f"{len(results)} requirements: {count['inconsistent']} inconsistent, {count['consistent']} consistent, "
          f"{count['no_documentation']} without documentation.", ""]
    for r in results:
        if r.get("verdict") != "inconsistent":
            continue
        md += [f"## {r['id']}", "", f"DOORS: {r['official']}", "", r.get("summary", ""), ""]
        md += [f"- **{i['type']}**: DOORS «{i['official']}» / document «{i['document']}» ({i['source']})"
               + (f". {i['explanation']}" if i.get("explanation") else "") for i in r["issues"]]
        md.append("")
    consistent = [r["id"] for r in results if r.get("verdict") == "consistent"]
    missing = [r["id"] for r in results if r.get("verdict") == "no_documentation"]
    md += ["## Consistent", "", ", ".join(consistent) or "-", ""]
    md += ["## Without documentation", "", ", ".join(missing) or "-"]
    return "\n".join(md) + "\n"


def verify_all(target: str, cfg: Config, llm: Any = None, graph=None, index=None, workers: int = 0) -> dict:
    """Verifies a requirement ID, every requirement of a DOORS module, or all of them ("*"),
    in parallel ([pipeline] workers), and writes <workspace>/reports/verify-<target>.json and
    .md. {"target", "results", "counts", "asked", "files"} or {"error"}."""
    v = Verifier(cfg, llm, graph, index)
    if target != "*" and v.graph._requirement(target)[0]:
        ids = [v.graph._requirement(target)[0]]
    else:
        ids, err = v.graph._module_requirements("" if target == "*" else target)
        if err:
            return {"error": f"'{target}' is neither a requirement nor a module", **{k: x for k, x in err.items()
                                                                                    if k == "modules"}}
        ids = [r for r in ids if v.graph.G.nodes[r].get("module")]
    with ThreadPoolExecutor(max(1, workers or cfg["pipeline"]["workers"])) as pool:
        results = list(pool.map(v.verify, ids))
    v.save_cache()
    folder = cfg.workspace / "reports"
    name = "all" if target == "*" else "".join(c if c.isalnum() or c in "-_" else "_" for c in target)
    files = [folder / f"verify-{name}.json", folder / f"verify-{name}.md"]
    atomic_write(files[0], json.dumps(results, ensure_ascii=False, indent=1))
    atomic_write(files[1], _report(target, results))
    return {"target": target, "results": results, "counts": {x: sum(r.get("verdict") == x for r in results)
                                                              for x in VERDICTS},
            "asked": v.asked, "files": [str(f) for f in files]}
