"""Hybrid search and cited answers (the retrieval part of legacy/doc_rag.py).

A search merges several rankings with Reciprocal Rank Fusion, which needs no calibration
between scores of different kinds:

- exact IDs (requirements, terms) mentioned in the query, matched against `ref`;
- dense: cosine similarity with the vectors of the current embeddings model;
- lexical: BM25 over the same texts (FTS5), with every token as an exact phrase.

Optionally the model first rewrites the question into 1-3 search queries plus exact
keywords. An answer takes the hits, replaces every chunk by itself and its neighbours (up
to a character budget) and asks the model to answer only from those numbered sources,
citing them as [n].
"""
from __future__ import annotations

import logging
from pathlib import Path

from atlas.index.embeddings import Embedder
from atlas.index.store import IndexStore
from atlas.llm.structured import JsonLlm

__all__ = ["answer", "build_context", "cite", "rrf", "search", "section_file"]

log = logging.getLogger(__name__)

CONTEXT_CHARS = 14000   # largest context passed to the model when answering

REWRITE_SYSTEM = """You turn a user question into search queries for a technical document \
collection. Reply with JSON: {"queries": [...], "keywords": [...]}. "queries": 1-3 short, \
self-contained search queries; if the question is not in English, include an English version, \
since technical documents are often written in English. "keywords": exact identifiers, acronyms \
or technical terms worth matching literally. JSON only."""

REWRITE_SCHEMA = {"type": "object", "properties": {
    "queries": {"type": "array", "items": {"type": "string"}},
    "keywords": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries", "keywords"]}

ANSWER_SYSTEM = """You answer questions using ONLY the numbered sources provided.
- Cite every factual statement with its source number in brackets, e.g. [2] or [1][3].
- Quote requirement identifiers and numeric values exactly as they appear.
- If the sources do not contain the answer, say so clearly and state what is missing; never \
use outside knowledge to fill gaps.
- If sources contradict each other, point it out.
- Answer in the same language as the question."""


def rrf(rankings: list[list[int]], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion: merges rankings without calibrating their scores."""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, rid in enumerate(ranking):
            score[rid] = score.get(rid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda r: -score[r])


def search(store: IndexStore, embedder: Embedder, query: str, k: int = 8, document: str = "",
           kinds: list[str] | None = None, llm: JsonLlm | None = None) -> list[dict]:
    """The k best records for `query`. `document` limits to a document (name, key or part of
    its title) and `kinds` to some kinds of record; with `llm`, the query is rewritten first."""
    allowed = store.allowed(document, kinds)
    queries, keywords = [query], []
    if llm is not None:
        try:
            d = llm.ask_json(REWRITE_SYSTEM, query, REWRITE_SCHEMA, "rewrite")
            queries += [q for q in d.get("queries", []) if isinstance(q, str) and q.strip()][:3]
            keywords = [w for w in d.get("keywords", []) if isinstance(w, str)][:8]
        except Exception as e:
            log.warning("query rewriting failed, going on with the original one: %s", e)
    n = max(k * 3, 20)
    rankings = [store.by_ref(" ".join([query] + keywords), allowed)]
    for q, qvec in zip(queries, embedder.queries(queries)):
        rankings.append(store.dense(qvec, embedder.model, n, allowed))
        rankings.append(store.lexical(q, n, allowed))
    if keywords:
        rankings.append(store.lexical(" ".join(keywords), n, allowed))
    return store.records(rrf([r for r in rankings if r])[:k])


def cite(r: dict) -> str:
    """'document › section, p. 3-4 (kind)'."""
    pages = ""
    if r.get("page_start"):
        pages = f", p. {r['page_start']}" + (f"-{r['page_end']}" if r["page_end"] != r["page_start"] else "")
    section = f" › {r['section_title']}" if r.get("section_title") else ""
    return f"{r['doc_name']}{section}{pages} ({r['kind']})"


def section_file(r: dict) -> str:
    """The Markdown file of the section of a record, or "" if there is none."""
    if not r.get("out_dir") or not r.get("section_id"):
        return ""
    hits = list((Path(r["out_dir"]) / "sections").glob(f"{r['section_id']}-*.md"))
    return str(hits[0]) if hits else ""


def build_context(store: IndexStore, hits: list[dict], max_chars: int = CONTEXT_CHARS) -> list[dict]:
    """Replaces every chunk by itself and its neighbours, without repeating, up to max_chars."""
    sources, used, total = [], set(), 0
    for h in hits:
        group = [r for r in store.neighbours(h) if r["id"] not in used]
        if not group:
            continue
        text = " … ".join(r["text"] for r in group)
        if total + len(text) > max_chars and sources:
            break
        used.update(r["id"] for r in group)
        total += len(text)
        sources.append({**h, "context": text})
    return sources


def answer(store: IndexStore, embedder: Embedder, llm: JsonLlm, question: str, k: int = 8,
           document: str = "", rewrite: bool = True) -> dict:
    """{"answer": text with [n] citations, "sources": [{n, cite, doc, section, file, kind, ref, excerpt}]}"""
    hits = search(store, embedder, question, k, document, llm=llm if rewrite else None)
    sources = build_context(store, hits)
    if not sources:
        return {"answer": "Nothing indexed is related to the question.", "sources": []}
    blocks = "\n\n".join(f"[{i}] {cite(s)}\n{s['context']}" for i, s in enumerate(sources, 1))
    text = llm.ask_text(ANSWER_SYSTEM, f"Sources:\n\n{blocks}\n\nQuestion: {question}")
    return {"answer": text,
            "sources": [{"n": i, "cite": cite(s), "doc": s["doc_name"], "section": s["section_id"],
                         "file": section_file(s), "kind": s["kind"], "ref": s.get("ref", ""),
                         "excerpt": s["text"][:300]} for i, s in enumerate(sources, 1)]}
