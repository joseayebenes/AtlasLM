"""Hybrid search and cited answers (the retrieval part of legacy/doc_rag.py).

A search merges several rankings with Reciprocal Rank Fusion, which needs no calibration
between scores of different kinds:

- exact IDs (requirements, terms) mentioned in the query, matched against `ref`;
- dense: cosine similarity with the vectors of the current embeddings model;
- lexical: BM25 over the same texts (FTS5), with every token as an exact phrase.

A search over every document keeps at most `per_document` hits of each document (or code
unit) while others have hits: otherwise the elements of one code unit, which all repeat
its name, can take every place.

Optionally the model first rewrites the question into 1-3 search queries plus exact
keywords, and says whether it is broad (it needs much of a document, not a few passages)
and which documents it is about. An answer from passages takes the hits, replaces every
chunk by itself and its neighbours (up to a character budget) and asks the model to
answer only from those numbered sources, citing them as [n]. An answer to a broad question
reads its documents whole, section by section: in one request if they fit in
`scope_chars`, and otherwise in parts, each giving notes that a last request combines.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from atlas.index.embeddings import Embedder
from atlas.index.records import CODE_PREFIX, DOORS_PREFIX
from atlas.index.store import IndexStore
from atlas.llm.structured import JsonLlm

__all__ = ["answer", "build_context", "cite", "diverse", "rewrite_question", "rrf", "search", "section_file",
           "whole_sources"]

log = logging.getLogger(__name__)

CONTEXT_CHARS = 14000   # largest context passed to the model when answering from passages
SCOPE_CHARS = 100000    # largest part of whole documents read in one request
SCOPE_DOCUMENTS = 3     # documents read whole when a broad question names none
PART_WORKERS = 4        # parts of whole documents read at the same time

REWRITE_SYSTEM = """You turn a user question into search queries for a technical document \
collection. Reply with JSON: {"queries": [...], "keywords": [...], "broad": true|false, "documents": [...]}.
- "queries": 1-3 short, self-contained search queries; if the question is not in English, include \
an English version, since technical documents are often written in English.
- "keywords": exact identifiers, acronyms or technical terms worth matching literally.
- "broad": true when the answer needs much of a document or of a requirements module, not a few \
passages: a summary or an explanation of a whole document, chapter or function, or every \
requirement, value or item about a topic. false for a specific fact, value, requirement or definition.
- "documents": the names, from the list of documents if there is one, of the documents or \
requirement modules the question is about; empty if it is about none in particular.
JSON only."""

REWRITE_SCHEMA = {"type": "object", "properties": {
    "queries": {"type": "array", "items": {"type": "string"}},
    "keywords": {"type": "array", "items": {"type": "string"}},
    "broad": {"type": "boolean"},
    "documents": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries", "keywords"]}

ANSWER_SYSTEM = """You answer questions using ONLY the numbered sources provided.
- Cite every factual statement with its source number in brackets, e.g. [2] or [1][3].
- Quote requirement identifiers and numeric values exactly as they appear.
- If the sources do not contain the answer, say so clearly and state what is missing; never \
use outside knowledge to fill gaps.
- If sources contradict each other, point it out.
- Answer in the same language as the question."""

WHOLE_SYSTEM = ANSWER_SYSTEM + """
- The sources are the whole documents the question is about, section by section, not a \
selection: cover everything in them that is relevant, in the order of the documents."""

NOTES_SYSTEM = """You read one part of some documents to help answer a question; the other \
parts are read separately. Write notes with EVERYTHING in this part that is relevant to the \
question: every fact, requirement identifier and value exactly as written, each with its source \
number in brackets, e.g. [12]. Do not answer the question and add nothing that is not in the \
sources. If nothing in this part is relevant, reply only: NOTHING"""

COMBINE_SYSTEM = """You answer a question from notes taken, part by part, over the whole \
documents it is about.
- Use ONLY the notes, and keep their source numbers as citations for every factual statement, \
e.g. [2] or [1][3].
- Quote requirement identifiers and numeric values exactly as they appear.
- Cover everything relevant in the notes, merging repetitions; if they contradict each other, \
point it out.
- If the notes do not contain the answer, say so clearly.
- Answer in the same language as the question."""


def rrf(rankings: list[list[int]], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion: merges rankings without calibrating their scores."""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, rid in enumerate(ranking):
            score[rid] = score.get(rid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda r: -score[r])


def rewrite_question(llm: JsonLlm, question: str, documents: list[dict] = ()) -> dict:
    """The question as search queries and exact keywords, whether it is broad and the
    documents it is about, among `documents` ({name, title}). If the model fails, the
    question as it is."""
    out = {"queries": [], "keywords": [], "broad": False, "documents": []}
    listed = "\n".join(f"- {d['name']}: {d['title']}" for d in documents)
    try:
        d = llm.ask_json(REWRITE_SYSTEM, f"Documents:\n{listed}\n\nQuestion: {question}" if listed else question,
                         REWRITE_SCHEMA, "rewrite")
    except Exception as e:
        log.warning("query rewriting failed, going on with the original one: %s", e)
        return out
    out["queries"] = [q for q in d.get("queries", []) if isinstance(q, str) and q.strip()][:3]
    out["keywords"] = [w for w in d.get("keywords", []) if isinstance(w, str)][:8]
    out["broad"] = d.get("broad") is True
    names = {key.lower(): doc["name"] for doc in documents for key in (doc["name"], doc["title"]) if key}
    found = [names.get(n.strip().lower()) for n in d.get("documents", []) if isinstance(n, str)]
    out["documents"] = list(dict.fromkeys(n for n in found if n))
    return out


def diverse(records: list[dict], k: int, per_document: int) -> list[dict]:
    """The first k records, with at most `per_document` of each document while others
    have some; in their order."""
    chosen, count = set(), Counter()
    for r in records:
        if count[r["doc"]] < per_document:
            chosen.add(r["id"])
            count[r["doc"]] += 1
            if len(chosen) == k:
                break
    for r in records:                       # few documents: the rest, in order
        if len(chosen) >= k:
            break
        chosen.add(r["id"])
    return [r for r in records if r["id"] in chosen]


def search(store: IndexStore, embedder: Embedder, query: str, k: int = 8, document: str = "",
           kinds: list[str] | None = None, llm: JsonLlm | None = None, dense: bool = True,
           per_document: int = 0, rewritten: dict | None = None) -> list[dict]:
    """The k best records for `query`. `document` limits to a document (name, key or part of
    its title) and `kinds` to some kinds of record; without `document`, `per_document` (0 =
    no limit) is the most hits of one document while others have some. With `llm`, the
    query is rewritten first (or `rewritten` gives it already). With dense=False there are
    no vectors (IDs and BM25 only): no request to any model."""
    allowed = store.allowed(document, kinds)
    if rewritten is None and llm is not None:
        rewritten = rewrite_question(llm, query)
    queries = [query] + (rewritten["queries"] if rewritten else [])
    keywords = rewritten["keywords"] if rewritten else []
    n = max(k * 3, 20)
    rankings = [store.by_ref(" ".join([query] + keywords), allowed)]
    for q, qvec in zip(queries, embedder.queries(queries) if dense else [None] * len(queries)):
        if dense:
            rankings.append(store.dense(qvec, embedder.model, n, allowed))
        rankings.append(store.lexical(q, n, allowed))
    if keywords:
        rankings.append(store.lexical(" ".join(keywords), n, allowed))
    ranked = rrf([r for r in rankings if r])
    if per_document and not document:
        return diverse(store.records(ranked), k, per_document)
    return store.records(ranked[:k])


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


def _without_overlap(previous: str, chunk: str) -> str:
    """A chunk without the text it repeats from the previous one (the overlap of chunk_text)."""
    for j in range(min(len(previous), len(chunk), 1000), 20, -1):
        if previous.endswith(chunk[:j]):
            return chunk[j:].lstrip()
    return chunk


def whole_sources(store: IndexStore, doc: str) -> list[dict]:
    """A document as sources, one per section and in its order: the text of its chunks
    without their overlap; in a DOORS module, its requirements and information objects;
    in a code unit, its elements. A section without any, its summary."""
    sections: dict[str, list[dict]] = {}
    for r in store.document_records(doc):
        if r["kind"] != "overview":
            sections.setdefault(r["section_id"] or "", []).append(r)
    document = not doc.startswith((DOORS_PREFIX, CODE_PREFIX))
    sources = []
    for records in sections.values():
        if document:            # the chunks hold the whole text, requirements included
            body = sorted((r for r in records if r["kind"] == "chunk"), key=lambda r: r["seq"])
        else:                   # in the order of the module or the unit
            body = [r for r in records if r["kind"] in ("chunk", "requirement", "code_doc")]
        body = body or [r for r in records if r["kind"] in ("section_summary", "requirement")]
        if not body:
            continue
        texts = [body[0]["text"]]
        for previous, r in zip(body, body[1:]):
            texts.append(_without_overlap(previous["text"], r["text"]) if document and r["kind"] == "chunk"
                         else r["text"])
        pages = [p for r in records for p in (r["page_start"], r["page_end"]) if p]
        sources.append({**records[0], "kind": "section", "ref": "", "page_start": min(pages, default=0),
                        "page_end": max(pages, default=0), "context": "\n".join(t for t in texts if t)})
    return sources


def _parts(blocks: list[str], max_chars: int) -> list[str]:
    """The numbered sources in parts of at most max_chars; a longer source is cut, and each
    piece keeps its header."""
    parts, current = [], ""
    for block in blocks:
        header, _, body = block.partition("\n")
        pieces = [block] if len(block) <= max_chars else \
            [f"{header}\n{body[i:i + max_chars - len(header) - 1]}" for i in range(0, len(body), max_chars - len(header) - 1)]
        for piece in pieces:
            if current and len(current) + len(piece) + 2 > max_chars:
                parts.append(current)
                current = ""
            current = f"{current}\n\n{piece}" if current else piece
    return parts + ([current] if current else [])


def answer_whole(store: IndexStore, llm: JsonLlm, question: str, docs: list[dict],
                 max_chars: int = SCOPE_CHARS) -> dict:
    """The answer from whole documents ({doc, name}): one request if they fit in max_chars;
    if not, notes from each part and a last request that combines them."""
    sources = [s for d in docs for s in whole_sources(store, d["doc"])]
    names = [d["name"] for d in docs]
    if not sources:
        return {"answer": "Nothing indexed is related to the question.", "sources": [], "scope": "whole",
                "documents": names, "requests": 0}
    blocks = [f"[{i}] {cite(s)}\n{s['context']}" for i, s in enumerate(sources, 1)]
    parts = _parts(blocks, max_chars)
    log.info("reading %s whole: %d sections, %d characters, %d parts", ", ".join(names), len(sources),
             sum(len(b) for b in blocks), len(parts))
    if len(parts) == 1:
        text = llm.ask_text(WHOLE_SYSTEM, f"Sources:\n\n{parts[0]}\n\nQuestion: {question}")
        requests, truncated = _requests(text), _truncated(text)
    else:
        with ThreadPoolExecutor(min(PART_WORKERS, len(parts))) as pool:
            notes = list(pool.map(lambda part: llm.ask_text(NOTES_SYSTEM, f"Sources:\n\n{part}\n\nQuestion: {question}"),
                                  parts))
        requests, truncated = sum(_requests(n) for n in notes), any(_truncated(n) for n in notes)
        notes = [n for n in notes if n.strip() and n.strip().rstrip(".").upper() != "NOTHING"]
        if not notes:
            text = f"Nothing in {', '.join(names)} is related to the question."
        else:
            joined = "\n\n".join(f"Part {i}:\n{n}" for i, n in enumerate(notes, 1))
            text = llm.ask_text(COMBINE_SYSTEM, f"Notes:\n\n{joined}\n\nQuestion: {question}")
            requests, truncated = requests + _requests(text), truncated or _truncated(text)
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
    return {"answer": str(text), "scope": "whole", "documents": names, "requests": requests,
            "sources": [_source(i, s) for i, s in enumerate(sources, 1) if i in cited],
            **({"truncated": True} if truncated else {})}


def _requests(text: str) -> int:
    """Requests an answer of ask_text took (two when it was asked again for being cut)."""
    return getattr(text, "requests", 1)


def _truncated(text: str) -> bool:
    """Whether an answer of ask_text is still cut by max_tokens."""
    return bool(getattr(text, "truncated", False))


def _source(n: int, s: dict) -> dict:
    return {"n": n, "cite": cite(s), "doc": s["doc_name"], "section": s["section_id"], "file": section_file(s),
            "kind": s["kind"], "ref": s.get("ref", ""),
            "excerpt": (s["context"] if s["kind"] == "section" else s["text"])[:300]}


def _scope_documents(store: IndexStore, document: str, named: list[str], hits: list[dict]) -> list[dict]:
    """The documents a broad question reads whole: `document`, else those it names, else
    those with most of its hits, up to SCOPE_DOCUMENTS and with at least half the hits of
    the first (not code units: their elements already are whole)."""
    docs = store.documents()
    if document:
        return [d for d in docs if document in (d["name"], d["doc"])] or \
               [d for d in docs if document.strip().lower() in d["title"].lower()][:1]
    if named:
        return [d for d in docs if d["name"] in named and not d["doc"].startswith(CODE_PREFIX)]
    by_doc = {d["doc"]: d for d in docs}
    ranked = Counter(h["doc"] for h in hits if not h["doc"].startswith(CODE_PREFIX)).most_common(SCOPE_DOCUMENTS)
    return [by_doc[doc] for doc, n in ranked if doc in by_doc and 2 * n >= ranked[0][1]]


def answer(store: IndexStore, embedder: Embedder, llm: JsonLlm, question: str, k: int = 8,
           document: str = "", rewrite: bool = True, kinds: list[str] | None = None, per_document: int = 0,
           context_chars: int = CONTEXT_CHARS, scope: str = "auto", scope_chars: int = SCOPE_CHARS) -> dict:
    """{"answer": text with [n] citations, "sources": [{n, cite, doc, section, file, kind, ref, excerpt}],
    "scope": "passages" | "whole", "documents": those read whole, "requests": to the model},
    plus "truncated": True if the answer is cut by max_tokens.
    scope "passages" answers from the best k passages; "whole" reads whole documents
    (`document`, or those of the question); "auto" reads them whole if the rewriting says
    the question is broad."""
    candidates = [d for d in store.documents() if not d["doc"].startswith(CODE_PREFIX)]
    rewritten = rewrite_question(llm, question, candidates) if rewrite else None
    requests = 1 if rewrite else 0
    if scope == "whole" or (scope == "auto" and rewritten and rewritten["broad"]):
        named = rewritten["documents"] if rewritten else []
        hits = [] if document or named else search(store, embedder, question, k, kinds=kinds,
                                                   per_document=per_document, rewritten=rewritten)
        docs = _scope_documents(store, document, named, hits)
        if docs:
            res = answer_whole(store, llm, question, docs, scope_chars)
            return {**res, "requests": res["requests"] + requests}
    hits = search(store, embedder, question, k, document, kinds, per_document=per_document, rewritten=rewritten)
    sources = build_context(store, hits, context_chars)
    if not sources:
        return {"answer": "Nothing indexed is related to the question.", "sources": [], "scope": "passages",
                "documents": [], "requests": requests}
    blocks = "\n\n".join(f"[{i}] {cite(s)}\n{s['context']}" for i, s in enumerate(sources, 1))
    text = llm.ask_text(ANSWER_SYSTEM, f"Sources:\n\n{blocks}\n\nQuestion: {question}")
    return {"answer": str(text), "sources": [_source(i, s) for i, s in enumerate(sources, 1)],
            "scope": "passages", "documents": [], "requests": requests + _requests(text),
            **({"truncated": True} if _truncated(text) else {})}
