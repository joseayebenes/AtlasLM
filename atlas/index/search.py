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
Before any request it makes a plan (documents, sections, characters, parts, requests) and
refuses to read more than `max_parts` parts: a vague question about a module of thousands
of requirements would take tens of requests of a quota shared by everyone.
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
from atlas.llm.connection import diagnose
from atlas.llm.structured import JsonLlm

__all__ = ["answer", "answer_whole", "build_context", "cite", "diverse", "rewrite_question", "rrf", "search",
           "section_file", "whole_plan", "whole_sources", "in_section", "asks_for"]

log = logging.getLogger(__name__)

CONTEXT_CHARS = 14000   # largest context passed to the model when answering from passages
SCOPE_CHARS = 100000    # largest part of whole documents read in one request
SCOPE_DOCUMENTS = 3     # documents read whole when a broad question names none
MAX_PARTS = 12          # most parts of whole documents read for one question
PART_WORKERS = 4        # parts of whole documents read at the same time
MAX_CHAPTERS = 50       # chapters listed to narrow a question that is too broad

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

MERGE_SYSTEM = """You merge notes taken, part by part, over some documents to answer a question \
into one set of notes; the question is answered later from them. Keep EVERY fact, requirement \
identifier and value exactly as written, each with its source numbers in brackets, e.g. [12]; \
merge only what is repeated. Do not answer the question and add nothing that is not in the notes."""


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


def asks_for(wanted: str, title: str) -> bool:
    """Whether `wanted` names a section with that title: the whole title, its number ("3",
    "2.1") or the title without its number, in any case."""
    norm = lambda t: " ".join(str(t).split()).lower().rstrip(".")
    wanted, title = norm(wanted), norm(title)
    number, _, rest = title.partition(" ")
    names = {title} | ({number, rest} if re.fullmatch(r"(\d+|[a-z])(\.\d+)*", number) and rest else set())
    return bool(wanted) and wanted in names


def in_section(r: dict, section: str) -> bool:
    """Whether a record is in a section or below it. `section` is the ID of the section
    ("004", or the heading of a DOORS module, "SRS-0910": only what is right under it); the
    title, the number or the title without number of a heading on the path of the record
    ("2.1", "Logging": with what is below it); or the start of its path, heading by heading
    and each one named the same ways ("Track management > Deletion"). "" = every record."""
    wanted = " ".join(section.split())
    if not wanted:
        return True
    if (r.get("section_id") or "").upper() == wanted.upper():
        return True
    path = [p.strip() for p in (r.get("path") or r.get("section_title") or "").split(" > ") if p.strip()]
    steps = [w.strip() for w in wanted.split(">")]
    if len(steps) == 1:
        return any(asks_for(wanted, t) for t in path)
    return len(steps) <= len(path) and all(asks_for(w, t) for w, t in zip(steps, path))


def whole_sources(store: IndexStore, doc: str, section: str = "") -> list[dict]:
    """A document as sources, one per section and in its order: the text of its chunks
    without their overlap; in a DOORS module, its requirements and information objects;
    in a code unit, its elements. A section without any, its summary. `section` keeps only
    a section and what is below it (see in_section)."""
    sections: dict[str, list[dict]] = {}
    for r in store.document_records(doc):
        if r["kind"] != "overview" and in_section(r, section):
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


def _with_minutes(plan: dict, requests_per_minute: int) -> dict:
    """The plan with the least minutes its requests take, if there is a limit per minute."""
    if requests_per_minute > 0:
        plan["minutes"] = round(plan["requests"] / requests_per_minute, 1)
    return plan


def whole_plan(store: IndexStore, docs: list[dict], max_chars: int = SCOPE_CHARS,
               extra_requests: int = 0, section: str = "") -> tuple[list[dict], list[str], dict]:
    """The sources of whole documents ({doc, name}), or of a section of them, their parts of
    at most max_chars and the plan of reading them, before any request: {documents,
    sections, chars, parts, requests} (and `section` if there is one). `requests` counts one
    per part and the last one that combines their notes (one in all if they fit in one
    part), plus `extra_requests` (the rewriting): the least, since notes that do not fit in
    one request are merged in groups first (see _combine)."""
    sources = [s for d in docs for s in whole_sources(store, d["doc"], section)]
    blocks = [f"[{i}] {cite(s)}\n{s['context']}" for i, s in enumerate(sources, 1)]
    parts = _parts(blocks, max_chars)
    reading = 0 if not parts else 1 if len(parts) == 1 else len(parts) + 1
    plan = {"documents": [d["name"] for d in docs], "sections": len(sources), "chars": sum(len(b) for b in blocks),
            "parts": len(parts), "requests": reading + extra_requests}
    if section:
        plan["section"] = section
    return sources, parts, plan


def _chapters(sources: list[dict]) -> list[dict]:
    """The first level of the sections of whole documents, with their characters: where to
    narrow a question that is too broad."""
    sizes: dict[tuple[str, str], int] = {}
    for s in sources:
        top = (s.get("path") or s.get("section_title") or "").split(" > ")[0].strip() or "(no section)"
        sizes[(s["doc_name"], top)] = sizes.get((s["doc_name"], top), 0) + len(s["context"])
    return [{"document": d, "section": t, "chars": n} for (d, t), n in sizes.items()][:MAX_CHAPTERS]


def answer_whole(store: IndexStore, llm: JsonLlm, question: str, docs: list[dict],
                 max_chars: int = SCOPE_CHARS, max_parts: int = MAX_PARTS, requests_per_minute: int = 0,
                 extra_requests: int = 0, section: str = "") -> dict:
    """The answer from whole documents ({doc, name}), or from a section of them: one request
    if they fit in max_chars; if not, notes from each part and a last request that combines
    them. With more than max_parts parts, or a section that is not there, it makes no
    request: an `error` with the plan and the chapters to narrow it to. `plan` and
    `requests` include `extra_requests` (the rewriting)."""
    sources, parts, plan = whole_plan(store, docs, max_chars, extra_requests, section)
    plan = _with_minutes(plan, requests_per_minute)
    names = plan["documents"]
    out = {"scope": "whole", "documents": names, "plan": plan}
    if not sources and section:
        return {**out, "error": f"there is no section '{section}' in {', '.join(names)}", "answer": "",
                "sources": [], "requests": extra_requests,
                "chapters": _chapters([s for d in docs for s in whole_sources(store, d["doc"])])}
    if not sources:
        return {**out, "answer": "Nothing indexed is related to the question.", "sources": [],
                "requests": extra_requests}
    log.info("reading %s whole: %d sections, %d characters, %d parts", ", ".join(names), plan["sections"],
             plan["chars"], plan["parts"])
    if len(parts) > max_parts:
        minutes = f", at least {plan['minutes']} min at {requests_per_minute} requests/min" if "minutes" in plan else ""
        return {**out, "error": f"reading {', '.join(names)} whole takes {plan['parts']} parts and "
                                f"{plan['requests']} requests to the model{minutes}, more than [search] "
                                f"max_parts = {max_parts}: narrow the question to a document or a section",
                "answer": "", "sources": [], "requests": extra_requests, "chapters": _chapters(sources)}
    failed: list[str] = []
    if len(parts) == 1:
        text, requests = llm.ask_text(WHOLE_SYSTEM, f"Sources:\n\n{parts[0]}\n\nQuestion: {question}"), 1
    else:
        notes, failed = _read_parts(llm, question, parts, names)
        requests = len(parts)
        if not notes:
            text = f"Nothing in {', '.join(names)} is related to the question."
        else:
            text, combining = _combine(llm, question, notes, max_chars)
            requests += combining
    text, cited = _renumber(text, len(sources))
    return {**out, "answer": text + "".join(f"\n\n({note})" for note in failed),
            "requests": requests + extra_requests,
            "sources": [_source(new, sources[old - 1]) for old, new in cited.items()],
            **({"failed_parts": failed} if failed else {})}


def _read_parts(llm: JsonLlm, question: str, parts: list[str], names: list[str]) -> tuple[list[str], list[str]]:
    """The notes of each part, PART_WORKERS at a time, without the empty ones, and a note
    ("part 2 of 4 failed: ...") for each part that failed; if all of them fail, the error."""
    with ThreadPoolExecutor(min(PART_WORKERS, len(parts))) as pool:
        futures = [pool.submit(llm.ask_text, NOTES_SYSTEM, f"Sources:\n\n{part}\n\nQuestion: {question}")
                   for part in parts]
    notes, failed, first_error = [], [], None
    for i, future in enumerate(futures, 1):
        try:
            note = future.result()
        except Exception as e:
            log.warning("part %d of %d of %s failed: %s", i, len(parts), ", ".join(names), e)
            failed.append(f"part {i} of {len(parts)} failed: {diagnose(e)}")
            first_error = first_error or e
            continue
        if note.strip() and note.strip().rstrip(".").upper() != "NOTHING":
            notes.append(note)
    if first_error is not None and len(failed) == len(parts):
        raise first_error
    return notes, failed


def _joined(notes: list[str]) -> str:
    return "\n\n".join(f"Part {i}:\n{n}" for i, n in enumerate(notes, 1))


def _combine(llm: JsonLlm, question: str, notes: list[str], max_chars: int) -> tuple[str, int]:
    """The answer from the notes of the parts and the requests it took. If the notes do not
    fit in max_chars, they are merged in groups that fit first, level by level, as the
    overview of a document (atlas.docs.analysis): their source numbers are those of the
    whole documents, so the citations are kept. The last request always fits: notes that
    cannot be grouped any more are cut."""
    requests, level = 0, 0
    while len(_joined(notes)) > max_chars and len(notes) > 1:
        groups, buf = [], []
        for n in notes:
            if buf and len(_joined(buf + [n])) > max_chars:
                groups.append(buf)
                buf = []
            buf.append(n)
        groups.append(buf)
        if len(groups) == len(notes):           # no two notes fit together
            break
        level += 1
        log.info("combining %d notes in %d groups (level %d)", len(notes), len(groups), level)
        merge = lambda g: g[0] if len(g) == 1 else \
            llm.ask_text(MERGE_SYSTEM, f"Notes:\n\n{_joined(g)}\n\nQuestion: {question}")
        with ThreadPoolExecutor(min(PART_WORKERS, len(groups))) as pool:
            notes = list(pool.map(merge, groups))
        requests += sum(len(g) > 1 for g in groups)
    if len(_joined(notes)) > max_chars:
        each = max(200, (max_chars - len(_joined([""] * len(notes)))) // len(notes))
        log.warning("the notes of %d parts do not fit in %d characters: each one cut to %d", len(notes), max_chars, each)
        notes = [n[:each] for n in notes]
    text = llm.ask_text(COMBINE_SYSTEM, f"Notes:\n\n{_joined(notes)}\n\nQuestion: {question}")
    return text, requests + 1


_CITATION = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")


def _renumber(text: str, count: int) -> tuple[str, dict[int, int]]:
    """The answer with its citations of sources 1..count numbered 1, 2, 3... in the order of
    the sources, and {old number: new}. "[4, 9]" counts as two citations."""
    numbers = lambda m: [int(n) for n in re.split(r"[,;]", m[1])]
    old = sorted({n for m in _CITATION.finditer(text) for n in numbers(m) if 1 <= n <= count})
    new = {n: i for i, n in enumerate(old, 1)}
    text = _CITATION.sub(lambda m: "[" + ", ".join(str(new.get(n, n)) for n in numbers(m)) + "]", text)
    return text, new


def _source(n: int, s: dict) -> dict:
    return {"n": n, "cite": cite(s), "doc": s["doc_name"], "section": s["section_id"], "file": section_file(s),
            "kind": s["kind"], "ref": s.get("ref", ""),
            "excerpt": (s["context"] if s["kind"] == "section" else s["text"])[:300]}


def _named(question: str, docs: list[dict]) -> list[str]:
    """The documents whose name (with or without its extension) or title the question
    says, without the model."""
    q = question.lower()
    keys = lambda d: {d["name"].lower(), Path(d["name"]).stem.lower(), d["title"].lower()}
    return [d["name"] for d in docs if any(len(k) >= 4 and k in q for k in keys(d))]


def _scope_documents(store: IndexStore, document: str, named: list[str], hits: list[dict],
                     most: int = SCOPE_DOCUMENTS) -> list[dict]:
    """The documents a broad question reads whole: `document`, else those it names, else
    those with most of its hits, up to `most` and with at least half the hits of the first
    (not code units: their elements already are whole)."""
    docs = store.documents()
    if document:
        return [d for d in docs if document in (d["name"], d["doc"])] or \
               [d for d in docs if document.strip().lower() in d["title"].lower()][:1]
    if named:
        return [d for d in docs if d["name"] in named and not d["doc"].startswith(CODE_PREFIX)]
    by_doc = {d["doc"]: d for d in docs}
    ranked = Counter(h["doc"] for h in hits if not h["doc"].startswith(CODE_PREFIX)).most_common(most)
    return [by_doc[doc] for doc, n in ranked if doc in by_doc and 2 * n >= ranked[0][1]]


def answer(store: IndexStore, embedder: Embedder, llm: JsonLlm | None, question: str, k: int = 8,
           document: str = "", rewrite: bool = True, kinds: list[str] | None = None, per_document: int = 0,
           context_chars: int = CONTEXT_CHARS, scope: str = "auto", scope_chars: int = SCOPE_CHARS,
           max_parts: int = MAX_PARTS, scope_documents: int = SCOPE_DOCUMENTS, requests_per_minute: int = 0,
           dry_run: bool = False, section: str = "") -> dict:
    """{"answer": text with [n] citations, "sources": [{n, cite, doc, section, file, kind, ref, excerpt}],
    "scope": "passages" | "whole", "documents": those read whole, "requests": to the model,
    "plan": of reading them whole}; with too many parts, an "error" instead of the answer
    (see answer_whole). scope "passages" answers from the best k passages; "whole" reads
    whole documents (`document`, or those of the question); "auto" reads them whole if the
    rewriting says the question is broad. `section` (see in_section) reads whole only that
    section of the documents, and what is below it, whatever the scope.
    dry_run makes no request to any model (llm can be None): the plan of reading whole the
    documents the question would read, chosen without the model (`document`, the documents
    whose name it says, or a lexical search), or None if there are none."""
    candidates = [d for d in store.documents() if not d["doc"].startswith(CODE_PREFIX)]
    if dry_run:
        named = _named(question, candidates)
        hits = [] if document or named else search(store, embedder, question, k, kinds=kinds,
                                                   per_document=per_document, dense=False)
        docs = _scope_documents(store, document, named, hits, scope_documents)
        plan = _with_minutes(whole_plan(store, docs, scope_chars, int(rewrite), section)[2], requests_per_minute) \
            if docs else None
        return {"answer": "", "sources": [], "scope": "whole" if docs else "passages",
                "documents": [d["name"] for d in docs], "requests": 0, "plan": plan,
                "passages_requests": int(rewrite) + 1, "max_parts": max_parts}
    rewritten = rewrite_question(llm, question, candidates) if rewrite else None
    requests = 1 if rewrite else 0
    if scope == "whole" or section or (scope == "auto" and rewritten and rewritten["broad"]):
        named = rewritten["documents"] if rewritten else []
        hits = [] if document or named else search(store, embedder, question, k, kinds=kinds,
                                                   per_document=per_document, rewritten=rewritten)
        docs = _scope_documents(store, document, named, hits, scope_documents)
        if docs:
            return answer_whole(store, llm, question, docs, scope_chars, max_parts, requests_per_minute, requests,
                                section)
    hits = search(store, embedder, question, k, document, kinds, per_document=per_document, rewritten=rewritten)
    sources = build_context(store, hits, context_chars)
    if not sources:
        return {"answer": "Nothing indexed is related to the question.", "sources": [], "scope": "passages",
                "documents": [], "requests": requests}
    blocks = "\n\n".join(f"[{i}] {cite(s)}\n{s['context']}" for i, s in enumerate(sources, 1))
    text = llm.ask_text(ANSWER_SYSTEM, f"Sources:\n\n{blocks}\n\nQuestion: {question}")
    return {"answer": text, "sources": [_source(i, s) for i, s in enumerate(sources, 1)],
            "scope": "passages", "documents": [], "requests": requests + 1}
