"""Proposals of traceability links: for a requirement without code, the code elements that
may implement it, judged by the agent req_linker. Nothing is written in the code: each
proposal says which `Req:` tag to add and where, and a person decides.

Two steps, as the verification:
1. Without a model, the candidates (at most MAX_CANDIDATES), each with the evidence that
   brought it:
   - the code elements that the document sections stating the requirement mention (graph);
   - the code that implements the requirements it is linked to (DOORS links, references);
   - the code elements that the index finds for its text (code_doc records), or for the
     text of the document that states it if it is not in DOORS.
   Each one goes with its comment, the requirements it already cites and the start of its
   code.
2. With the model, the judgement: the agent req_linker (its Markdown file gives the
   instructions, the model and the reasoning) picks the candidates that implement the
   requirement, with a confidence (high, medium, low) and a reason; it may pick none.
   Whatever it picks that was not a candidate is dropped.

A requirement without candidates is "no_candidates", without asking the model. The
judgements are kept in <workspace>/reports/.link_cache.json by the hash of the evidence, the
instructions and the model: proposing again only asks for what changed.

    res = link("SRS-0005", cfg)              # {"id", "verdict", "links", "candidates", ...}
    report = link_all("SRS_TMS", cfg)         # every requirement without code of a module
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from atlas.agents.definitions import AgentDef
from atlas.agents.guard import data_block
from atlas.config import Config
from atlas.pipeline import atomic_write, sha256_text

__all__ = ["CONFIDENCES", "candidates", "link", "link_all"]

log = logging.getLogger(__name__)

CONFIDENCES = ("high", "medium", "low")
VERDICTS = ("proposed", "none", "no_candidates")
CODE_KINDS = ("subprogram", "type")
MAX_CANDIDATES = 6
MAX_CODE_CHARS = 1000
SCHEMA = {"type": "object", "required": ["links", "summary"], "properties": {
    "links": {"type": "array", "items": {"type": "object", "required": ["code", "confidence", "reason"],
                                         "properties": {"code": {"type": "string"},
                                                        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                                                        "reason": {"type": "string"}}}},
    "summary": {"type": "string"}}}


def _declarations(store) -> dict[tuple[str, str], str]:
    """Where each subprogram is declared in a spec, "file:line", by (unit, name)."""
    out = {}
    for u in store.code_units():
        for d in u.get("declarations", []):
            out.setdefault((u["unit"], d["name"]), f"{u['file']}:{d['line']}")
    return out


def candidates(req_id: str, graph, index=None, declarations: dict | None = None) -> dict:
    """The requirement and its candidates, without a model: {"id", "official", "module",
    "documents", "linked", "implemented_by", "candidates": [{"id", "kind", "where", "tag_at",
    "doc", "requirements", "code", "evidence"}]} or {"error"}."""
    t = graph.trace(req_id)
    if "error" in t:
        return t
    rid, found = t["id"], {}
    current = [x["id"] for x in t["implemented_by"]["items"]] if t.get("implemented_by") else []

    def add(n: str, why: str) -> None:
        if n in graph.G and graph.G.nodes[n].get("kind") in CODE_KINDS and n not in current:
            reasons = found.setdefault(n, [])
            if why not in reasons:
                reasons.append(why)

    for x in t["documents"]:                                   # 1. what the sections that state it mention
        for n in graph._linked(x["id"], {"mentions"}, incoming=False):
            add(n, f"mentioned in {x['document']} › {x['title']}, which states {rid}")
    linked = [{"id": x["id"], "type": x["type"], "text": graph.G.nodes[x["id"]].get("text", "")}
              for x in t["links_out"] + t["links_in"] if x["id"] in graph.G]
    for x in linked:                                           # 2. the code of the requirements linked to it
        for n in graph._code_of(x["id"]):
            add(n, f"implements {x['id']}, linked to {rid} ({x['type']})")
    text = t.get("text") or next((x["text"] for x in t["documents"] if x.get("text")), "")
    if index is not None and text:                             # 3. what the search finds in the code
        try:
            hits = index.search(text, k=MAX_CANDIDATES, kinds=["code_doc"])
        except Exception as e:                                 # no embeddings server: the graph is enough
            log.warning("link %s: no search in the code (%s)", rid, e)
            hits = []
        for rank, h in enumerate(hits, 1):
            add(h["ref"], f"found by searching the code for its text (result {rank})")

    declarations = declarations or {}
    out = []
    for n, evidence in sorted(found.items(), key=lambda kv: -len(kv[1]))[:MAX_CANDIDATES]:
        d = graph.G.nodes[n]
        start = f"{d.get('file', '')}:{d.get('start_line') or d.get('line', '')}"
        where = f"{start}-{d['end_line']}" if d.get("start_line") else start
        source = graph.read_source(n)
        code = source.get("source", "") if "error" not in source else ""
        out.append({"id": n, "kind": d.get("kind"), "where": where,
                    "tag_at": declarations.get((d.get("unit"), d.get("name"))) or start,
                    "doc": d.get("doc", ""), "requirements": d.get("requirements", []),
                    "code": code[:MAX_CODE_CHARS] + ("\n…" if len(code) > MAX_CODE_CHARS else ""),
                    "evidence": evidence})
    return {"id": rid, "official": t.get("text", ""), "module": t.get("module", ""),
            "documents": [{"source": f"{x['document']} › {x['title']}", "text": x.get("text", "")}
                          for x in t["documents"]][:3],
            "linked": linked, "implemented_by": current, "candidates": out}


def _prompt(c: dict) -> str:
    lines = [f"Requirement {c['id']}" + (f" (module {c['module']})" if c["module"] else ""),
             f"Official text (DOORS): {c['official'] or '(not in DOORS)'}"]
    lines += [f"Stated in {d['source']}: {d['text']}" for d in c["documents"] if d["text"]]
    lines += [f"Linked: {x['type']} {x['id']}" + (f": {x['text']}" if x["text"] else "") for x in c["linked"]]
    lines.append("\nCandidates:")
    for i, x in enumerate(c["candidates"], 1):
        lines += [f"\n[{i}] {x['id']} ({x['kind']}, {x['where']})",
                  "Why it is a candidate: " + "; ".join(x["evidence"]),
                  f"Comment: {x['doc'] or '(none)'}",
                  f"Already cites: {', '.join(x['requirements']) or '(nothing)'}",
                  f"Code:\n{x['code']}" if x["code"] else "Code: (not available)"]
    return data_block("\n".join(lines), f"requirement {c['id']} and its candidates")


def _clean(answer: dict, offered: dict[str, dict]) -> dict:
    """The answer of the model with the shape of SCHEMA, keeping only offered candidates."""
    links, seen = [], set()
    for x in answer.get("links") or []:
        code = str(x.get("code") or "").strip().lower() if isinstance(x, dict) else ""
        if code in offered and code not in seen:
            seen.add(code)
            links.append({"code": code, "confidence": x.get("confidence") if x.get("confidence") in CONFIDENCES
                          else "low", "reason": str(x.get("reason") or "")})
    order = {c: i for i, c in enumerate(CONFIDENCES)}
    links.sort(key=lambda x: order[x["confidence"]])
    return {"links": links, "summary": str(answer.get("summary") or "")}


class Linker:
    """Proposes links with the agent req_linker, with the cache of judgements."""

    def __init__(self, cfg: Config, llm: Any = None, graph=None, index=None):
        from atlas.agents.runtime import agent_service, definition
        from atlas.graph import KnowledgeGraph
        from atlas.store import Store
        self.cfg = cfg
        self.agent: AgentDef = definition("req_linker", cfg)
        self.service = agent_service(self.agent, cfg)
        self._llm = llm
        self.graph = graph or KnowledgeGraph.from_config(cfg)
        self.index = index
        self.declarations = _declarations(Store(cfg.workspace))
        tag = (cfg["sources"].get("code") or {}).get("req_tag") or "Req:"
        self.tag = lambda rid: f"-- {tag} {rid}"
        self.cache_path = cfg.workspace / "reports" / ".link_cache.json"
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

    def link(self, req_id: str) -> dict:
        c = candidates(req_id, self.graph, self.index, self.declarations)
        if "error" in c:
            return {"id": req_id, **c}
        offered = {x["id"]: x for x in c["candidates"]}
        base = {"id": c["id"], "official": c["official"], "implemented_by": c["implemented_by"],
                "candidates": [{k: x[k] for k in ("id", "kind", "where", "evidence")} for x in c["candidates"]]}
        if not offered:
            return {**base, "verdict": "no_candidates", "links": [],
                    "summary": "No code element is mentioned with it, implements a linked requirement or "
                               "comes up when searching the code for its text."}
        prompt = _prompt(c)
        key = sha256_text(prompt, self.agent.instruction, self.service.model)[:24]
        with self._lock:
            judged = self.cache.get(key)
        if judged is None:
            judged = _clean(self.llm.ask_json(self.agent.instruction, prompt, SCHEMA, "links"), offered)
            with self._lock:
                self.cache[key] = judged
                self.asked += 1
        links = [{**x, "where": offered[x["code"]]["where"], "tag_at": offered[x["code"]]["tag_at"],
                  "tag": self.tag(c["id"]), "evidence": offered[x["code"]]["evidence"]} for x in judged["links"]]
        return {**base, "verdict": "proposed" if links else "none", "links": links, "summary": judged["summary"]}

    def save_cache(self) -> None:
        atomic_write(self.cache_path, json.dumps(self.cache, ensure_ascii=False, indent=1))


def link(req_id: str, cfg: Config, llm: Any = None, graph=None, index=None) -> dict:
    """The proposals for one requirement (see the module docstring)."""
    linker = Linker(cfg, llm, graph, index)
    res = linker.link(req_id)
    linker.save_cache()
    return res


def _report(target: str, results: list[dict]) -> str:
    count = {v: sum(r.get("verdict") == v for r in results) for v in VERDICTS}
    by_confidence = {c: sum(1 for r in results for x in r.get("links", []) if x["confidence"] == c)
                     for c in CONFIDENCES}
    md = [f"# Proposed traceability links: {target}", "",
          f"{len(results)} requirements: {count['proposed']} with a proposal "
          f"({', '.join(f'{n} {c}' for c, n in by_confidence.items() if n) or 'none'}), "
          f"{count['none']} without a convincing candidate, {count['no_candidates']} without candidates.", "",
          "Nothing has been changed in the code. To accept a proposal, add its tag to the header "
          "comment of that element; the next `atlas run` reads it.", ""]
    for r in results:
        if r.get("verdict") != "proposed":
            continue
        md += [f"## {r['id']}", "", f"DOORS: {r['official']}", "", r.get("summary", ""), ""]
        for x in r["links"]:
            md += [f"- **{x['code']}** ({x['confidence']}): {x['reason']}",
                   f"  Add `{x['tag']}` at {x['tag_at']}. Evidence: {'; '.join(x['evidence'])}."]
        md.append("")
    md += ["## Without a convincing candidate", "", ", ".join(r["id"] for r in results if r.get("verdict") == "none")
           or "-", "", "## Without candidates", "",
           ", ".join(r["id"] for r in results if r.get("verdict") == "no_candidates") or "-"]
    return "\n".join(md) + "\n"


def link_all(target: str, cfg: Config, llm: Any = None, graph=None, index=None, workers: int = 0) -> dict:
    """Proposes links for a requirement ID, for the requirements without code of a DOORS
    module, or of all of them ("*"), in parallel ([pipeline] workers), and writes
    <workspace>/reports/link-<target>.json and .md. {"target", "results", "counts", "asked",
    "files"} or {"error"}."""
    linker = Linker(cfg, llm, graph, index)
    g = linker.graph
    if target != "*" and g._requirement(target)[0]:
        ids = [g._requirement(target)[0]]
    else:
        reqs, err = g._module_requirements("" if target == "*" else target)
        if err:
            return {"error": f"'{target}' is neither a requirement nor a module", **{k: x for k, x in err.items()
                                                                                    if k == "modules"}}
        ids = g._without_code([r for r in reqs if g.G.nodes[r].get("module")])
    with ThreadPoolExecutor(max(1, workers or cfg["pipeline"]["workers"])) as pool:
        results = list(pool.map(linker.link, ids))
    linker.save_cache()
    folder = cfg.workspace / "reports"
    name = "all" if target == "*" else "".join(c if c.isalnum() or c in "-_" else "_" for c in target)
    files = [folder / f"link-{name}.json", folder / f"link-{name}.md"]
    atomic_write(files[0], json.dumps(results, ensure_ascii=False, indent=1))
    atomic_write(files[1], _report(target, results))
    return {"target": target, "results": results,
            "counts": {v: sum(r.get("verdict") == v for r in results) for v in VERDICTS},
            "asked": linker.asked, "files": [str(f) for f in files]}
