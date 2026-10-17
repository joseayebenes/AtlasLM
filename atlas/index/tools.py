"""Document tools for agents and the MCP server (the MCP of legacy/doc_rag.py).

    list_documents    the indexed documents and DOORS modules, with their summary
    search            hybrid search in the documents and in the requirements of DOORS
                      (exact IDs + meaning + keywords), narrowed by document, module,
                      chapter, kind of passage and DOORS attributes
    get_section       the Markdown of a section
    get_requirement   a requirement by its ID: its official text (DOORS) and the documents
                      that state it
    ask_docs          an answer written only from the documents, citing them; a broad
                      question (or whole=True) reads its documents whole

`route(question)` is not a tool: it gives the sources that need no search when a question
names requirement IDs or code and asks for their traceability ("which test verifies
SRS-0004?"), from DOORS and, if there is one, the graph. ask_docs and `atlas ask` pass
them to the answer before the passages.

Every answer is a compact JSON-serialisable dict; a problem (a document or section that is
not there, the model server down) is an answer with `error`, never an exception, so that
the agent can correct itself or tell the user.

    tools = DocTools.from_config(cfg)
    tools.search("alarm latency", k=5)
    tools.search("deletion", module="SRS_TMS", attribute="status=Approved")
    tools.get_requirement("SRS-0102")

They are registered as "tool" plugins with these names, for [agents.*] tools and [mcp] tools.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import openai

from atlas.index.records import DOORS_PREFIX, KINDS, asks_for
from atlas.index.search import citation_warnings, cite, filters
from atlas.index.store import REF_PATTERN
from atlas.llm import model_failed
from atlas.plugins import register_methods
from atlas.store import Store

if TYPE_CHECKING:
    from atlas.index import Index

__all__ = ["DocTools", "TOOL_NAMES"]

TOOL_NAMES = ("list_documents", "search", "get_section", "get_requirement", "ask_docs")

MAX_RESULTS = 20            # results of a search
MAX_SUMMARY_CHARS = 500     # characters of the summary of a document in list_documents
MAX_SECTION_CHARS = 20000   # characters of a section
ROUTE_CHARS = 4000          # characters of a source of route
ROUTE_IDS = 3               # requirements and code elements of a question that route follows
MAX_FILTER_VALUES = 10      # values of each filter when a filter matches nothing

# words of a question about traceability (English and Spanish), by what they ask for
TRACE_WORDS = re.compile(r"\b(test|prueba|verif|ensay|implement|parent|padre|child|hij|deriv|satisf|link|enlace"
                         r"|trace|traza|call|llama)", re.I)
IMPACT_WORDS = re.compile(r"\b(impact|afecta|cambi|chang|modif)", re.I)


def _heading(f: Path) -> dict:
    """id, title, titles of the subsections merged into it and path (the titles above it) of
    a section file, from its front matter."""
    out = {"id": f.name.split("-", 1)[0], "title": "", "includes": [], "path": []}
    with open(f, encoding="utf-8") as lines:
        if lines.readline().strip() == "---":
            for line in lines:
                key, _, value = line.partition(":")
                if line.strip() == "---":
                    break
                if key in ("title", "includes", "path"):
                    try:
                        out[key] = json.loads(value)
                    except ValueError:
                        pass
    return out


def _routed(doc_name: str, title: str, kind: str, ref: str, text: str) -> dict:
    """A source of route, with the fields of an index record that answers use."""
    text = text if len(text) <= ROUTE_CHARS else text[:ROUTE_CHARS] + " …"
    return {"doc": "", "doc_name": doc_name, "section_id": "", "section_title": title, "kind": kind, "ref": ref,
            "page_start": 0, "page_end": 0, "out_dir": "", "text": text, "context": text}


def _official(r: dict) -> str:
    """A requirement of Store.requirement as the text of a source."""
    links = lambda items, end: ", ".join(f"{x[end]} ({x['type']})" for x in items)
    lines = [f"{r['id']} (DOORS module {r['module']})", r["text"] or ""]
    if r.get("parent"):
        lines.append(f"parent: {r['parent']}")
    if r.get("attributes"):
        lines.append("attributes: " + "; ".join(f"{k} = {v}" for k, v in r["attributes"].items()))
    if r.get("links_out"):
        lines.append("links out: " + links(r["links_out"], "target"))
    if r.get("links_in"):
        lines.append("links in: " + links(r["links_in"], "source"))
    return "\n".join(lines)


class DocTools:
    def __init__(self, index: "Index", store: Store | None = None, cfg=None, graph=None):
        self.index = index
        self.store = store          # the requirements read from DOORS (atlas.db)
        self.cfg = cfg              # for the variables it lacks, when the model fails
        self.graph = graph          # GraphTools, for route; None: only DOORS
        convention = (((cfg.get("sources") or {}).get("doors") or {}).get("convention") or {}) if cfg else {}
        self._id_re = re.compile(convention["id_pattern"]) if convention.get("id_pattern") else None

    @classmethod
    def from_config(cls, cfg, store: Store | None = None, graph=None) -> "DocTools":
        """Over the atlas.db of the workspace, with the [embeddings] and [llm] of atlas.toml,
        and its graph (GraphTools) if atlas.graph can be imported."""
        from atlas.index import Index
        if graph is None:
            try:
                from atlas.graph.tools import GraphTools
                graph = GraphTools.from_config(cfg)
            except ImportError:
                graph = None
        return cls(Index.from_config(cfg), store or Store(cfg.workspace), cfg, graph)

    @classmethod
    def for_context(cls, context: Any) -> "DocTools":
        """One instance per context, shared by every tool plugin."""
        tools = getattr(context, "_doc_tools", None)
        if tools is None:
            tools = cls.from_config(context.cfg, getattr(context, "store", None))
            context._doc_tools = tools
        return tools

    def list_documents(self) -> dict:
        """The indexed documents and DOORS modules: name (what `document` takes in search
        and get_section), title, source (document | DOORS), number of indexed passages and
        summary."""
        docs = self.index.documents()
        return {"count": len(docs),
                "documents": [{"name": d["name"], "title": d["title"],
                               "source": "DOORS" if d["doc"].startswith(DOORS_PREFIX) else "document",
                               "records": d["records"],
                               "summary": str((d["overview"] or {}).get("summary", ""))[:MAX_SUMMARY_CHARS]}
                              for d in docs]}

    def search(self, query: str, k: int = 0, document: str = "", kind: str = "", attribute: str = "",
               module: str = "", chapter: str = "") -> dict:
        """Hybrid search in the documents, in the requirements of DOORS and in the code:
        exact IDs, meaning and keywords. `k` results (0: the configured number, 8 by default;
        at most a few of each document unless `document` is given). `document` limits it to
        one document, DOORS module or code unit (its name or part of its title); `kind` to
        one kind of passage: chunk | section_summary | requirement | definition | open_issue |
        overview | code_doc (a subprogram, type or variable with its comment and code).
        Filters, combined: `module`, a DOORS module ("SRS_TMS"); `chapter`, a chapter of a
        document or module, by its number or title ("3", "Logging"); `attribute`, values of
        the `meta` of the results, "name=value" and several separated by ";": the DOORS
        attributes of a requirement ("status=Approved;verification=Test"), or "kind=test",
        "file=tracks.adb" in the code. Each result has its citation, its document and section
        (for get_section; for a DOORS requirement, get_requirement with its ref; for code,
        read_source or trace_code with its ref), its text and its meta. A filter that matches
        nothing answers with the names and values the filters can take."""
        if kind and kind not in KINDS:
            return {"error": f"unknown kind '{kind}'", "kinds": list(KINDS)}
        try:
            where = filters(attribute, module, chapter)
        except ValueError as e:
            return {"error": str(e)}
        try:
            hits = self.index.search(query, k=min(k, MAX_RESULTS) if k > 0 else 0, document=document,
                                     kinds=[kind] if kind else None, where=where or None)
        except openai.APIError as e:
            return model_failed(e, self.cfg)
        out: dict[str, Any] = {"results": [{"cite": cite(h), "document": h["doc_name"], "section": h["section_id"],
                                            "kind": h["kind"], "ref": h.get("ref", ""), "text": h["text"],
                                            **({"meta": h["meta"]} if h.get("meta") else {})} for h in hits]}
        if where and not hits:
            known = self.index.meta_values()
            unknown = [key for key in where if key not in known]
            out["filters"] = {key: values[:MAX_FILTER_VALUES] for key, values in known.items()}
            if unknown:
                out["error"] = f"no passage has {', '.join(unknown)}: the filters are {', '.join(known) or 'none'}"
        return out

    def get_section(self, document: str, section: str) -> dict:
        """The whole Markdown of a section (summary, requirements and its original text).
        `document` and `section` as a search result gives them ("004"), or the number or
        title the section has in the document ("3", "2.1", "Logging"); a chapter made of
        several sections answers with the list of them."""
        docs = self.index.documents()
        found = [d for d in docs if document in (d["name"], d["doc"])] or \
                [d for d in docs if document.strip() and document.strip().lower() in d["title"].lower()]
        if not found:
            return {"error": f"document '{document}' is not indexed", "documents": [d["name"] for d in docs]}
        if len(found) > 1:
            return {"error": f"'{document}' is ambiguous", "documents": [d["name"] for d in found]}
        d = found[0]
        if d["doc"].startswith(DOORS_PREFIX):
            return {"error": f"{d['name']} is a DOORS module, not a document: get_requirement gives a "
                             f"requirement of it by its ID"}
        files = sorted((Path(d["out_dir"]) / "sections").glob("*.md"))
        heads = {f: _heading(f) for f in files}
        hits = [f for f in files if heads[f]["id"] == section.strip()] or \
               [f for f in files if asks_for(section, heads[f]["title"])] or \
               [f for f in files if any(asks_for(section, t) for t in heads[f]["includes"])] or \
               [f for f in files if any(asks_for(section, t) for t in heads[f]["path"])]
        if len(hits) != 1:
            listed = [f"{heads[f]['id']}: {heads[f]['title']}" for f in (hits or files)]
            return {"error": f"section '{section}' is " + ("ambiguous" if hits else f"not in {d['name']}"),
                    "sections": listed[:100]}
        h, text = heads[hits[0]], hits[0].read_text(encoding="utf-8")
        return {"document": d["name"], "section": h["id"], "title": h["title"], "file": str(hits[0]),
                "markdown": text[:MAX_SECTION_CHARS], "truncated": len(text) > MAX_SECTION_CHARS}

    def get_requirement(self, req_id: str) -> dict:
        """A requirement by its exact ID: its official text, module, attributes, parent and
        links in the requirements database (DOORS), when its module was exported, and the passages of the documents that
        state it, with their citation."""
        official = self.store.requirement(req_id.strip()) if self.store else None
        in_docs = [h for h in self.index.requirement(req_id) if not h["doc"].startswith(DOORS_PREFIX)]
        if not official and not in_docs:
            return {"error": f"requirement '{req_id}' not found",
                    "similar": self.store.requirement_ids(req_id) if self.store else []}
        out: dict[str, Any] = {"id": official["id"] if official else in_docs[0]["ref"]}
        if official:
            out["official"] = {k: official[k] for k in ("module", "text", "parent", "attributes", "links_out",
                                                         "links_in")}
            exported = self.store.modules_info().get(official["module"], {}).get("exported_at", "") if self.store else ""
            if exported:
                out["official"]["exported_at"] = exported
        out["documents"] = [{"cite": cite(h), "document": h["doc_name"], "section": h["section_id"],
                             "text": h["text"]} for h in in_docs]
        return out

    def route(self, question: str) -> list[dict]:
        """The sources a question about traceability needs, without searching: if it names
        requirement IDs (those of the [sources.doors.convention] id_pattern, or any in DOORS
        or the graph) or code elements (a unit, or a name with "." or "_") and asks for
        tests, implementation, parents, links, calls or the impact of a change. Of each
        requirement, its trace in the graph (or, without it, its official text, parent and
        links in DOORS) and its impact; of each code element, its trace_code and its impact.
        [] when the question is not about traceability."""
        impact, trace = bool(IMPACT_WORDS.search(question)), bool(TRACE_WORDS.search(question))
        if not (impact or trace):
            return []
        g = self.graph.graph() if self.graph is not None else None
        sources: list[dict] = []
        tokens = list(dict.fromkeys(t.rstrip(".-_") for t in REF_PATTERN.findall(question)))
        requirements = [t.upper() for t in tokens if re.search(r"[A-Za-z]", t) and re.search(r"\d", t)]
        if self._id_re is not None:
            requirements = [r for r in requirements if self._id_re.fullmatch(r)]
        for rid in requirements[:ROUTE_IDS]:
            traced = g.trace(rid) if g is not None else {"error": "no graph"}
            if "error" not in traced:
                sources.append(_routed("traceability graph", f"trace {traced['id']}", "trace", traced["id"],
                                       json.dumps(traced, ensure_ascii=False)))
            else:
                official = self.store.requirement(rid) if self.store else None
                if official:
                    sources.append(_routed(f"DOORS {official['module']}", official["id"], "requirement",
                                           official["id"], _official(official)))
            if impact and g is not None and "error" not in (affected := g.impact(rid)):
                sources.append(_routed("traceability graph", f"impact {rid}", "impact", rid,
                                       json.dumps(affected, ensure_ascii=False)))
        if g is None:
            return sources
        from atlas.graph.build import CODE_KINDS
        code = []
        for t in dict.fromkeys(re.findall(r"[A-Za-z_][\w.]*\w", question)):
            n, err = g.resolve(t)
            kind = g.G.nodes[n].get("kind") if n else None
            if not err and n not in code and (kind == "unit" or (kind in CODE_KINDS and ("." in t or "_" in t))):
                code.append(n)
        for n in code[:ROUTE_IDS]:
            if trace:
                sources.append(_routed("traceability graph", f"trace_code {n}", "trace", n,
                                       json.dumps(g.trace_code(n), ensure_ascii=False)))
            if impact:
                sources.append(_routed("traceability graph", f"impact {n}", "impact", n,
                                       json.dumps(g.impact(n), ensure_ascii=False)))
        return sources

    def ask_docs(self, question: str, document: str = "", whole: bool = False, section: str = "") -> dict:
        """An answer written by the model only from the documents, citing them as [n], with
        the list of sources. Slower than search: use it when the answer needs several
        passages. `document` limits it to one document or DOORS module. A broad question (a
        summary, a whole function, every requirement about a topic) reads its documents
        whole instead of the best passages, in parts if they are long; `whole` = true makes
        it read them whole always (`scope` in the answer says how it was read). If that takes
        more parts than allowed, the answer is an `error` with the plan and the chapters of
        the documents: ask again with a `document` and a `section`. `section` reads whole only
        a section and what is below it: its number or title ("2.1", "Logging"), or its ID as a
        search result gives it. A question about the tests, code, parent, links or impact of
        a requirement or code element also gets their traceability from DOORS and the graph
        as its first sources. `status` says whether it answers: "answered" (every figure and
        ID with its source), "partial" (some sentences without a source, in `uncited`, or an
        answer that says what is missing) or "not_found" (nothing related is indexed); a
        partial answer must be given as such. `sources` are those the answer cites;
        `unsupported` lists the sentences with an ID or a value that the sources they cite do
        not contain, and `warnings` says so in a line."""
        try:
            res = self.index.answer(question, document=document, scope="whole" if whole else "",
                                    extra_sources=self.route(question), section=section)
        except openai.APIError as e:
            return model_failed(e, self.cfg)
        warnings = citation_warnings(res)
        return {**res, "warnings": warnings} if warnings else res


register_methods(DocTools, TOOL_NAMES)
