"""Document tools for agents and the MCP server (the MCP of legacy/doc_rag.py).

    list_documents    the indexed documents, with their summary
    search            hybrid search in the documents (exact IDs + meaning + keywords)
    get_section       the Markdown of a section
    get_requirement   a requirement by its ID: its official text (DOORS) and the documents
                      that state it
    ask_docs          an answer written only from the documents, citing them

Every answer is a compact JSON-serialisable dict; a problem (a document or section that is
not there, the model server down) is an answer with `error`, never an exception, so that
the agent can correct itself or tell the user.

    tools = DocTools.from_config(cfg)
    tools.search("alarm latency", k=5)
    tools.get_requirement("SRS-0102")

They are registered as "tool" plugins with these names, for [agents.*] tools and [mcp] tools.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import openai

from atlas.index.records import KINDS
from atlas.index.search import cite
from atlas.llm.connection import diagnose
from atlas.plugins import register_methods
from atlas.store import Store

if TYPE_CHECKING:
    from atlas.index import Index

__all__ = ["DocTools", "TOOL_NAMES"]

TOOL_NAMES = ("list_documents", "search", "get_section", "get_requirement", "ask_docs")

MAX_RESULTS = 20            # results of a search
MAX_SUMMARY_CHARS = 500     # characters of the summary of a document in list_documents
MAX_SECTION_CHARS = 20000   # characters of a section


def _failed(e: openai.APIError) -> dict:
    return {"error": f"the model server failed: {diagnose(e)}"}


class DocTools:
    def __init__(self, index: "Index", store: Store | None = None):
        self.index = index
        self.store = store          # the requirements read from DOORS (atlas.db)

    @classmethod
    def from_config(cls, cfg, store: Store | None = None) -> "DocTools":
        """Over the atlas.db of the workspace, with the [embeddings] and [llm] of atlas.toml."""
        from atlas.index import Index
        return cls(Index.from_config(cfg), store or Store(cfg.workspace))

    @classmethod
    def for_context(cls, context: Any) -> "DocTools":
        """One instance per context, shared by every tool plugin."""
        tools = getattr(context, "_doc_tools", None)
        if tools is None:
            tools = cls.from_config(context.cfg, getattr(context, "store", None))
            setattr(context, "_doc_tools", tools)
        return tools

    def list_documents(self) -> dict:
        """The indexed documents: name (what `document` takes in search and get_section),
        title, number of indexed passages and summary."""
        docs = self.index.documents()
        return {"count": len(docs),
                "documents": [{"name": d["name"], "title": d["title"], "records": d["records"],
                               "summary": str((d["overview"] or {}).get("summary", ""))[:MAX_SUMMARY_CHARS]}
                              for d in docs]}

    def search(self, query: str, k: int = 8, document: str = "", kind: str = "") -> dict:
        """Hybrid search in the documents: exact IDs, meaning and keywords. `document` limits
        it to one document (its name or part of its title); `kind` to one kind of passage:
        chunk | section_summary | requirement | definition | open_issue | overview. Each
        result has its citation, its document and section (for get_section) and its text."""
        if kind and kind not in KINDS:
            return {"error": f"unknown kind '{kind}'", "kinds": list(KINDS)}
        try:
            hits = self.index.search(query, k=max(1, min(k, MAX_RESULTS)), document=document,
                                     kinds=[kind] if kind else None)
        except openai.APIError as e:
            return _failed(e)
        return {"results": [{"cite": cite(h), "document": h["doc_name"], "section": h["section_id"],
                             "kind": h["kind"], "ref": h.get("ref", ""), "text": h["text"]} for h in hits]}

    def get_section(self, document: str, section: str) -> dict:
        """The whole Markdown of a section (summary, requirements and its original text), by
        the `document` and `section` of a search result."""
        docs = self.index.documents()
        found = [d for d in docs if document in (d["name"], d["doc"])] or \
                [d for d in docs if document.strip() and document.strip().lower() in d["title"].lower()]
        if not found:
            return {"error": f"document '{document}' is not indexed", "documents": [d["name"] for d in docs]}
        if len(found) > 1:
            return {"error": f"'{document}' is ambiguous", "documents": [d["name"] for d in found]}
        d = found[0]
        sid = section.strip().zfill(3) if section.strip().isdigit() else section.strip()
        files = sorted((Path(d["out_dir"]) / "sections").glob("*.md"))
        hits = [f for f in files if f.name.startswith(f"{sid}-")]
        if not hits:
            return {"error": f"section '{section}' is not in {d['name']}",
                    "sections": [f.stem for f in files][:100]}
        text = hits[0].read_text(encoding="utf-8")
        return {"document": d["name"], "section": sid, "file": str(hits[0]), "markdown": text[:MAX_SECTION_CHARS],
                "truncated": len(text) > MAX_SECTION_CHARS}

    def get_requirement(self, req_id: str) -> dict:
        """A requirement by its exact ID: its official text, module, attributes, parent and
        links in the requirements database (DOORS), and the passages of the documents that
        state it, with their citation."""
        official = self.store.requirement(req_id.strip()) if self.store else None
        in_docs = self.index.requirement(req_id)
        if not official and not in_docs:
            return {"error": f"requirement '{req_id}' not found",
                    "similar": self.store.requirement_ids(req_id) if self.store else []}
        out: dict[str, Any] = {"id": official["id"] if official else in_docs[0]["ref"]}
        if official:
            out["official"] = {k: official[k] for k in ("module", "text", "parent", "attributes", "links_out",
                                                         "links_in")}
        out["documents"] = [{"cite": cite(h), "document": h["doc_name"], "section": h["section_id"],
                             "text": h["text"]} for h in in_docs]
        return out

    def ask_docs(self, question: str, document: str = "") -> dict:
        """An answer written by the model only from the documents, citing them as [n], with
        the list of sources. Slower than search: use it when the answer needs several
        passages. `document` limits it to one document."""
        try:
            return self.index.answer(question, document=document)
        except openai.APIError as e:
            return _failed(e)


register_methods(DocTools, TOOL_NAMES)
