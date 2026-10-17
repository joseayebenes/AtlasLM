"""Hybrid search and cited answers (the retrieval part of legacy/doc_rag.py).

A search merges several rankings with Reciprocal Rank Fusion, which needs no calibration
between scores of different kinds:

- exact IDs (requirements, terms) mentioned in the query, matched against `ref`, and the
  glossary definitions of its acronyms (2 to 6 capitals: "TMF"). An ID written in another
  way ("srs0004", "SRS_0004", "srs-4") is first written as in the index ("SRS-0004"), with
  the prefixes of the indexed requirements and their number of digits (ids_in);
- dense: cosine similarity with the vectors of the current embeddings model;
- lexical: BM25 over the same texts (FTS5), with every token as an exact phrase; with
  [search] synonyms, a query that has a term of a group adds one more lexical ranking,
  of the query with the other terms of the group.

A search can be narrowed to a document, to some kinds of record and by the meta of the
records (`where`): the attributes and the module of a DOORS requirement, the chapter of a
passage, the kind and the file of a code element (see records.py).

The vector of a query is kept in the embeddings cache of the index (by the model, its query
prefix and the text), so the same query asked again makes no request to the embeddings.

A search over every document keeps at most `per_document` hits of each document (or code
unit) while others have hits: otherwise the elements of one code unit, which all repeat
its name, can take every place.

Optionally the model first rewrites the question into 1-3 search queries plus exact
keywords, and says whether it is broad (it needs much of a document, not a few passages)
and which documents it is about. An answer from passages takes the hits, replaces every
one by its context (IndexStore.neighbours: the adjacent chunks, the records around a DOORS
requirement, the first chunks of a section summary, the passage that states a requirement;
up to a character budget) and asks the model to answer only from those numbered sources,
citing them as [n]. An answer to a broad question
reads its documents whole, section by section: in one request if they fit in
`scope_chars`, and otherwise in parts, each giving notes that a last request combines.

Every answer has its citations checked without a model (check_citations): a [n] of a
source that does not exist, and the IDs and values with a unit of a sentence that none of
the sources it cites contains, are in the result; `sources` are only those cited. Its
`status` says, without a model either, whether it answers (answer_status): "not_found",
"answered" or "partial", with the sentences that cite no source in `uncited`.

`extra_sources` (DocTools.route: the traceability of the requirements and code the
question names, from DOORS and the graph) go before the passages as the first numbered
sources; a question that has them is not rewritten, nor read whole unless scope="whole".

Before reading whole documents it makes a plan (documents, sections, characters, parts,
requests) and refuses to read more than `max_parts` parts: a vague question about a module
of thousands of requirements would take tens of requests of a quota shared by everyone.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from atlas.index.embeddings import Embedder
from atlas.index.records import CODE_PREFIX, DOORS_PREFIX, asks_for
from atlas.index.store import REF_PATTERN, IndexStore
from atlas.llm.connection import diagnose
from atlas.llm.structured import JsonLlm

__all__ = ["acronyms", "answer", "answer_status", "answer_whole", "build_context", "check_citations", "cite",
           "citation_warnings", "diverse", "expand_synonyms", "filters", "grounded_status", "ids_in", "in_section",
           "query_vectors", "rewrite_question", "rrf", "search", "section_file", "status_line", "whole_plan",
           "whole_sources"]

log = logging.getLogger(__name__)

CONTEXT_CHARS = 14000   # largest context passed to the model when answering from passages
CONTEXT_RADIUS = 1      # records on each side of a hit in its context (IndexStore.neighbours)
SCOPE_CHARS = 100000    # largest part of whole documents read in one request
SCOPE_DOCUMENTS = 3     # documents read whole when a broad question names none
MAX_PARTS = 12          # most parts of whole documents read for one question
PART_WORKERS = 4        # parts of whole documents read at the same time
MAX_CHAPTERS = 50       # chapters listed to narrow a question that is too broad
SUMMARY_KINDS = ["overview", "section_summary"]   # what chooses the documents of a broad question

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


NOTHING_FOUND = "Nothing indexed is related to the question."

CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
SENTENCE_END = re.compile(r"[.!?](?:\s*\[\d+(?:\s*,\s*\d+)*\])*(?=\s+[^\s\da-z]|\s*$)")
LIST_MARK = re.compile(r"^\s*(?:[-*•>]|#+|\d+[.)])\s+")
UNITS = (r"ms|msecs?|milliseconds?|milisegundos?|s|secs?|seconds?|segundos?|min|mins|minutes?|minutos?|h|hrs?"
         r"|hours?|horas?|days?|d[ií]as?|weeks?|semanas?|Hz|kHz|MHz|GHz|mm|cm|m|km|metres?|meters?|metros?|NM"
         r"|ft|feet|pies|kts?|kn|knots?|nudos?|deg|degrees?|grados?|°|dB|dBm|mV|mA|kW|bits?|bytes?|KB|kB|MB|GB"
         r"|bps|kbps|Mbps|%|percent")
NUMBER_UNIT = re.compile(rf"(?<![\w.,])\d+(?:[.,]\d+)?\s?(?:{UNITS})(?!\w)")
UNIT_NAMES = {**dict.fromkeys(["msec", "msecs", "millisecond", "milliseconds", "milisegundo", "milisegundos"], "ms"),
              **dict.fromkeys(["sec", "secs", "second", "seconds", "segundo", "segundos"], "s"),
              **dict.fromkeys(["mins", "minute", "minutes", "minuto", "minutos"], "min"),
              **dict.fromkeys(["hr", "hrs", "hour", "hours", "hora", "horas"], "h"),
              **dict.fromkeys(["day", "days", "dia", "dias", "día", "días"], "d"),
              **dict.fromkeys(["week", "weeks", "semana", "semanas"], "wk"),
              **dict.fromkeys(["metre", "metres", "meter", "meters", "metro", "metros"], "m"),
              **dict.fromkeys(["feet", "pies"], "ft"),
              **dict.fromkeys(["kt", "kts", "knot", "knots", "nudo", "nudos"], "kn"),
              **dict.fromkeys(["degree", "degrees", "grado", "grados"], "deg")}


def _canon(s: str) -> str:                                  # as in atlas/docs/analysis.py
    return re.sub(r"\W+", " ", s.replace("-\n", "")).lower().strip()


def _norm(text: str) -> str:
    """A text canonized to look for IDs and values in it: words in lower case, the
    punctuation as spaces, the number apart from its unit and the units by one name."""
    text = re.sub(r"(\d)\s?%", r"\1 percent", text.replace("°", " deg"))
    text = re.sub(r"(?<=\d)(?=[^\W\d_])", " ", text)
    return " ".join(UNIT_NAMES.get(w, w) for w in _canon(text).split())


def _sentences(text: str) -> list[str]:
    """The sentences of an answer: by line, without their list marks, and by full stop; a
    citation after the full stop stays with its sentence."""
    out = []
    for line in text.splitlines():
        line, start = LIST_MARK.sub("", line).strip(), 0
        for m in SENTENCE_END.finditer(line):
            out.append(line[start:m.end()].strip())
            start = m.end()
        out.append(line[start:].strip())
    return [s for s in out if s.strip("*_#|-: ")]


def _cited(sentence: str) -> list[int]:
    return list(dict.fromkeys(int(n) for group in CITATION.findall(sentence) for n in re.split(r"\s*,\s*", group)))


def _claims(sentence: str) -> list[tuple[str, str]]:
    """The IDs and the values with a unit of a sentence, as written and canonized."""
    bare = CITATION.sub(" ", sentence)
    found = [t.rstrip(".-_") for t in REF_PATTERN.findall(bare)]
    found = [t for t in found if re.search(r"[^\W\d_]", t) and re.search(r"\d", t)] + NUMBER_UNIT.findall(bare)
    return list({_norm(t): t for t in found}.items())


def check_citations(text: str, sources: list[dict]) -> dict:
    """The citations of an answer against its numbered sources (dicts with `context`, the
    text the model read), without a model: {"cited": the numbers that exist, "out_of_range":
    those that do not, "unsupported": [{claim, cited, missing}]}, the sentences with an ID
    or a value with a unit (320 ms, 30 days, 95 %) that no source they cite contains, after
    canonizing both texts."""
    texts: dict[int, str] = {}
    cited, out_of_range, unsupported = set(), set(), []
    for sentence in _sentences(text):
        numbers = _cited(sentence)
        valid = [n for n in numbers if 1 <= n <= len(sources)]
        out_of_range.update(set(numbers) - set(valid))
        cited.update(valid)
        if not numbers:
            continue
        for n in valid:
            if n not in texts:
                s = sources[n - 1]
                texts[n] = f" {_norm(cite(s) + chr(10) + s['context'])} "
        missing = [t for key, t in _claims(sentence) if not any(f" {key} " in texts[n] for n in valid)]
        if missing:
            unsupported.append({"claim": sentence, "cited": numbers, "missing": missing})
    return {"cited": sorted(cited), "out_of_range": sorted(out_of_range), "unsupported": unsupported}


def answer_status(text: str, consulted: int, found: bool = True) -> dict:
    """Whether an answer with [n] citations answers, from its text alone: {"status",
    "uncited": the sentences without a citation, "consulted": the sources it was given}.
    "not_found": no sources, `found` false or the fixed answer of nothing indexed;
    "answered": at least one citation and no sentence without one that has a figure or an
    ID; "partial": the rest (an answer that says what the sources lack, or facts without
    their source)."""
    if not consulted or not found or text.strip() == NOTHING_FOUND:
        return {"status": "not_found", "uncited": [], "consulted": consulted}
    sentences = _sentences(text)
    uncited = [s for s in sentences if not CITATION.search(s)]
    answered = len(uncited) < len(sentences) and not any(re.search(r"\d", s) for s in uncited)
    return {"status": "answered" if answered else "partial", "uncited": uncited, "consulted": consulted}


def grounded_status(text: str, results: list[str]) -> dict:
    """answer_status of an answer that cites by ID and not by [n] (that of an agent), against
    the texts it was given (its tool results): `uncited` are the sentences with an ID or a
    value with a unit that none of them contains. "not_found": no answer or no results;
    "answered": some sentence with an ID or a value, and every one found; "partial": the rest."""
    if not text.strip() or not results:
        return {"status": "not_found", "uncited": [], "consulted": len(results)}
    texts = [f" {_norm(r)} " for r in results]
    found, uncited = False, []
    for sentence in _sentences(text):
        claims = _claims(sentence)
        if any(not any(f" {key} " in t for t in texts) for key, _ in claims):
            uncited.append(sentence)
        found = found or bool(claims)
    return {"status": "answered" if found and not uncited else "partial", "uncited": uncited,
            "consulted": len(results)}


def status_line(res: dict) -> str:
    """'status: partial (2 sentences without a source)'."""
    n = len(res.get("uncited", []))
    return f"status: {res['status']}" + (f" ({n} sentence{'s' if n != 1 else ''} without a source)" if n else "")


def citation_warnings(res: dict) -> list[str]:
    """What check_citations found in an answer, in a line each."""
    out, n = [], len(res.get("unsupported", []))
    if n:
        out.append(f"{n} claim{'s' if n != 1 else ''} not supported by {'their' if n != 1 else 'its'} sources")
    if res.get("out_of_range"):
        out.append("citations of sources that do not exist: " + " ".join(f"[{i}]" for i in res["out_of_range"]))
    return out


def filters(attribute: str = "", module: str = "", chapter: str = "") -> dict[str, str]:
    """The `where` of a search from the text of its options: `attribute` is
    "status=Approved", several separated by ";" ("status=Approved;verification=Test");
    `module` a DOORS module and `chapter` a chapter (its number or its title). Keys in
    lower case. ValueError if a pair has no "=", no key or no value."""
    where: dict[str, str] = {}
    for pair in (attribute or "").split(";"):
        if not pair.strip():
            continue
        key, eq, value = pair.partition("=")
        if not eq or not key.strip() or not value.strip():
            raise ValueError(f"attribute '{pair.strip()}' is not name=value (e.g. status=Approved; several "
                             f"separated by ';')")
        where[key.strip().lower()] = value.strip()
    for key, value in (("module", module), ("chapter", chapter)):
        if value and value.strip():
            where[key] = value.strip()
    return where


def rrf(rankings: list[list[int]], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion: merges rankings without calibrating their scores."""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, rid in enumerate(ranking):
            score[rid] = score.get(rid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda r: -score[r])


_ACRONYM = re.compile(r"(?<![\w\-.])[A-Z]{2,6}(?![\w\-])")


def acronyms(text: str) -> list[str]:
    """The acronyms of a text: words of 2 to 6 capitals ("TMF", "HMI"), not part of an ID
    ("SRS-0004")."""
    return list(dict.fromkeys(_ACRONYM.findall(text)))


def ids_in(query: str, prefixes: dict[str, int], normalize: str = "upper",
           known: Callable[[list[str]], set[str]] | None = None) -> list[tuple[str, str]]:
    """The requirement IDs of a query however they are written: (as written, ID). `prefixes`
    are those of the index with their number of digits (IndexStore.id_prefixes): a prefix,
    then a space, nothing, "-" or "_", then the number, padded with zeros to that width
    ("srs 4" -> "SRS-0004"). `normalize` is [sources.doors.convention] id_normalize (upper |
    none); with `known` (IndexStore.known_refs), only the IDs that exist."""
    found = []
    for prefix, width in prefixes.items():
        for m in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(prefix)}[ _-]?(\d+)(?!\d)", query, re.I):
            rid = f"{prefix}-{(m.group(1).lstrip('0') or '0').zfill(width)}"
            found.append((m.group(0), rid.upper() if normalize == "upper" else rid))
    if found and known is not None:
        exists = known([rid for _, rid in found])
        found = [(written, rid) for written, rid in found if rid.upper() in exists]
    return found


def _words(text: str) -> list[str]:
    """The words of a text in lower case, without accents and without a plural "s"."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    return [w.removesuffix("s") if len(w) > 3 else w for w in re.findall(r"\w+", plain)]


def expand_synonyms(query: str, groups: list[list[str]]) -> str:
    """The query with the other terms of every group ([search] synonyms) that has one of its
    terms in it (any case, accent or plural "s"); "" if no group has any."""
    words, added = _words(query), []
    for group in groups:
        found = [t for t in group if (tw := _words(t)) and any(words[i:i + len(tw)] == tw
                                                                for i in range(len(words) - len(tw) + 1))]
        if found:
            added += [t for t in group if t not in found and t not in added]
    return f"{query} {' '.join(added)}" if added else ""


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


def query_vectors(store: IndexStore, embedder: Embedder, texts: list[str]) -> list:
    """The vectors of some queries: those in the cache from there, the others in one request,
    which then go to the cache (a cache that cannot be written is not an error)."""
    keys = [embedder.query_key(t) for t in texts]
    vectors = store.cache_get(list(dict.fromkeys(keys)))
    missing = list(dict.fromkeys(t for t, k in zip(texts, keys) if k not in vectors))
    if missing:
        new = dict(zip((embedder.query_key(t) for t in missing), embedder.queries(missing)))
        try:
            store.cache_put(new)
        except sqlite3.Error as e:
            log.debug("the vectors of the queries are not cached: %s", e)
        vectors.update(new)
    return [vectors[k] for k in keys]


def search(store: IndexStore, embedder: Embedder, query: str, k: int = 8, document: str = "",
           kinds: list[str] | None = None, llm: JsonLlm | None = None, dense: bool = True,
           per_document: int = 0, rewritten: dict | None = None, where: dict | None = None,
           synonyms: list[list[str]] = (), id_normalize: str = "upper") -> list[dict]:
    """The k best records for `query`. `document` limits to a document (name, key or part of
    its title), `kinds` to some kinds of record and `where` to the records whose meta has
    those values ({"status": "Approved", "chapter": "3"}, see IndexStore.allowed); without
    `document`, `per_document` (0 = no limit) is the most hits of one document while others
    have some. With `llm`, the query is rewritten first (or `rewritten` gives it already).
    With dense=False there are no vectors (IDs and BM25 only): no request to any model.
    `synonyms`: groups of equivalent terms (config.read_synonyms); `id_normalize`, that of
    the IDs (ids_in)."""
    allowed = store.allowed(document, kinds, where)
    for written, rid in ids_in(query, store.id_prefixes(), id_normalize, store.known_refs):
        query = re.sub(rf"(?<![A-Za-z0-9]){re.escape(written)}(?!\d)", rid, query)   # "srs-4": as "SRS-0004"
    if rewritten is None and llm is not None:
        rewritten = rewrite_question(llm, query)
    queries = [query] + (rewritten["queries"] if rewritten else [])
    keywords = rewritten["keywords"] if rewritten else []
    n = max(k * 3, 20)
    exact = " ".join([query] + keywords)
    ids = store.by_ref(exact, allowed)
    rankings = [ids + [i for i in store.definitions(acronyms(exact), allowed) if i not in ids]]
    for q, qvec in zip(queries, query_vectors(store, embedder, queries) if dense else [None] * len(queries)):
        if dense:
            rankings.append(store.dense(qvec, embedder.model, n, allowed))
        rankings.append(store.lexical(q, n, allowed))
        expanded = expand_synonyms(q, synonyms) if synonyms else ""
        if expanded:
            rankings.append(store.lexical(expanded, n, allowed))
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


def build_context(store: IndexStore, hits: list[dict], max_chars: int = CONTEXT_CHARS,
                  radius: int = CONTEXT_RADIUS) -> list[dict]:
    """Replaces every hit by its context (IndexStore.neighbours), without repeating, up to
    max_chars. A piece from another document (a DOORS requirement stated in a document)
    goes with its citation."""
    sources, used, total = [], set(), 0
    for h in hits:
        group = [r for r in store.neighbours(h, radius) if r["id"] not in used]
        if not group:
            continue
        text = " … ".join(r["text"] if r["doc"] == h["doc"] else f"({cite(r)}) {r['text']}" for r in group)
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


def whole_sources(store: IndexStore, doc: str, section: str = "", allowed: set[int] | None = None) -> list[dict]:
    """A document as sources, one per section and in its order: the text of its chunks
    without their overlap; in a DOORS module, its requirements and information objects;
    in a code unit, its elements. A section without any, its summary. `section` keeps only
    a section and what is below it (see in_section); `allowed` (IndexStore.allowed), only
    those records."""
    sections: dict[str, list[dict]] = {}
    for r in store.document_records(doc):
        if r["kind"] != "overview" and in_section(r, section) and (allowed is None or r["id"] in allowed):
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


def whole_plan(store: IndexStore, docs: list[dict], max_chars: int = SCOPE_CHARS, extra_requests: int = 0,
               section: str = "", extra_sources: list[dict] = (),
               allowed: set[int] | None = None) -> tuple[list[dict], list[str], dict]:
    """The sources of whole documents ({doc, name}), or of a section of them, after
    `extra_sources`, their parts of at most max_chars and the plan of reading them, before
    any request: {documents, sections, chars, parts, requests} (and `section` if there is
    one). `requests` counts one per part and the last one that combines their notes (one in
    all if they fit in one part), plus `extra_requests` (the rewriting): the least, since
    notes that do not fit in one request are merged in groups first (see _combine).
    `allowed` (IndexStore.allowed) keeps only those records."""
    sources = list(extra_sources) + [s for d in docs for s in whole_sources(store, d["doc"], section, allowed)]
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
                 extra_requests: int = 0, section: str = "", extra_sources: list[dict] = (),
                 where: dict | None = None) -> dict:
    """The answer from whole documents ({doc, name}), or from a section of them, after
    `extra_sources`: one request if they fit in max_chars; if not, notes from each part and
    a last request that combines them. With more than max_parts parts, or a section that is
    not there, it makes no request: an `error` with the plan and the chapters to narrow it
    to. `plan` and `requests` include `extra_requests` (the rewriting). A part that fails
    leaves a note ("part 2 of 4 failed: ...") at the end of the answer, the answer is
    written with the others and its status is partial. With `where`, only the records of
    the documents that match it (see IndexStore.allowed). The citations are numbered 1, 2,
    3... in the order of the sources."""
    allowed = store.allowed(where=where) if where else None
    sources, parts, plan = whole_plan(store, docs, max_chars, extra_requests, section, extra_sources, allowed)
    plan = _with_minutes(plan, requests_per_minute)
    names = plan["documents"]
    out = {"scope": "whole", "documents": names, "plan": plan}
    if not sources and section:
        return {**_checked("", [], False, False), **out, "error": f"there is no section '{section}' in "
                f"{', '.join(names)}", "requests": extra_requests,
                "chapters": _chapters([s for d in docs for s in whole_sources(store, d["doc"])])}
    if not sources:
        return {**_checked(NOTHING_FOUND, [], False), **out, "requests": extra_requests}
    log.info("reading %s whole: %d sections, %d characters, %d parts", ", ".join(names), plan["sections"],
             plan["chars"], plan["parts"])
    if len(parts) > max_parts:
        minutes = f", at least {plan['minutes']} min at {requests_per_minute} requests/min" if "minutes" in plan else ""
        return {**_checked("", [], False, False), **out,
                "error": f"reading {', '.join(names)} whole takes {plan['parts']} parts and "
                         f"{plan['requests']} requests to the model{minutes}, more than [search] "
                         f"max_parts = {max_parts}: narrow the question to a document or a section",
                "requests": extra_requests, "chapters": _chapters(sources)}
    failed: list[str] = []
    found = True
    if len(parts) == 1:
        text = llm.ask_text(WHOLE_SYSTEM, f"Sources:\n\n{parts[0]}\n\nQuestion: {question}")
        requests, truncated = _requests(text), _truncated(text)
    else:
        notes, failed, requests, truncated = _read_parts(llm, question, parts, names)
        found = bool(notes)
        if not notes:
            text = f"Nothing in {', '.join(names)} is related to the question."
        else:
            text, combining = _combine(llm, question, notes, max_chars)
            requests, truncated = requests + combining, truncated or _truncated(text)
    text, cited = _renumber(str(text), len(sources))
    ordered = [sources[old - 1] for old in cited] + [s for i, s in enumerate(sources, 1) if i not in cited]
    res = {**_checked(text, ordered, False, found), **out, "requests": requests + extra_requests,
           **({"truncated": True} if truncated else {}), **({"failed_parts": failed} if failed else {})}
    if failed:
        res["answer"] += "".join(f"\n\n({note})" for note in failed)
        res["status"] = "partial"
    return res


def _requests(text: str) -> int:
    """Requests an answer of ask_text took (two when it was asked again for being cut)."""
    return getattr(text, "requests", 1)


def _truncated(text: str) -> bool:
    """Whether an answer of ask_text is still cut by max_tokens."""
    return bool(getattr(text, "truncated", False))


def _checked(text: str, sources: list[dict], passages: bool, found: bool = True) -> dict:
    """The answer, its status, the sources it cites and the check of its citations; from
    passages, the sources it does not cite in `unused`."""
    check = check_citations(text, sources)
    cited = set(check["cited"])
    out = {"answer": text, **answer_status(text, len(sources), found),
           "sources": [_source(i, s) for i, s in enumerate(sources, 1) if i in cited],
           "unsupported": check["unsupported"], "out_of_range": check["out_of_range"]}
    if passages:
        out["unused"] = [{"n": i, "cite": cite(s)} for i, s in enumerate(sources, 1) if i not in cited]
    return out


def _read_parts(llm: JsonLlm, question: str, parts: list[str],
                names: list[str]) -> tuple[list[str], list[str], int, bool]:
    """The notes of each part, PART_WORKERS at a time, without the empty ones, a note ("part
    2 of 4 failed: ...") for each part that failed, the requests they took and whether some
    note is cut by max_tokens; if all of them fail, the error."""
    with ThreadPoolExecutor(min(PART_WORKERS, len(parts))) as pool:
        futures = [pool.submit(llm.ask_text, NOTES_SYSTEM, f"Sources:\n\n{part}\n\nQuestion: {question}")
                   for part in parts]
    notes: list[str] = []
    failed: list[str] = []
    requests, truncated, first_error = 0, False, None
    for i, future in enumerate(futures, 1):
        try:
            note = future.result()
        except Exception as e:
            log.warning("part %d of %d of %s failed: %s", i, len(parts), ", ".join(names), e)
            failed.append(f"part {i} of {len(parts)} failed: {diagnose(e)}")
            first_error, requests = first_error or e, requests + 1
            continue
        requests, truncated = requests + _requests(note), truncated or _truncated(note)
        if note.strip() and note.strip().rstrip(".").upper() != "NOTHING":
            notes.append(note)
    if first_error is not None and len(failed) == len(parts):
        raise first_error
    return notes, failed, requests, truncated


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
    return text, requests + _requests(text)


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
    """A source of an answer; `context` is the whole text the model read from it."""
    return {"n": n, "cite": cite(s), "doc": s["doc_name"], "section": s["section_id"], "file": section_file(s),
            "kind": s["kind"], "ref": s.get("ref", ""),
            "excerpt": (s["context"] if s["kind"] == "section" else s["text"])[:300], "context": s["context"]}


def _named(question: str, docs: list[dict]) -> list[str]:
    """The documents whose name (with or without its extension) or title the question
    says, without the model."""
    q = question.lower()
    keys = lambda d: {d["name"].lower(), Path(d["name"]).stem.lower(), d["title"].lower()}
    return [d["name"] for d in docs if any(len(k) >= 4 and k in q for k in keys(d))]


def _scope_hits(store: IndexStore, embedder: Embedder, question: str, k: int, kinds: list[str] | None,
                per_document: int, rewritten: dict | None, dense: bool = True, where: dict | None = None,
                synonyms: list[list[str]] = (), id_normalize: str = "upper") -> list[dict]:
    """The hits that choose the documents of a broad question that names none: those of the
    summaries of the documents and of their sections, which say what a whole document or
    chapter is about better than a few passages. If no document has summaries (DOORS
    modules, documents analysed without the model), the hits of the question. `where`,
    `synonyms` and `id_normalize` as in search."""
    lexicon = {"where": where, "synonyms": synonyms, "id_normalize": id_normalize}
    hits = search(store, embedder, question, max(k, 8), kinds=SUMMARY_KINDS, rewritten=rewritten, dense=dense,
                  **lexicon)
    hits = [h for h in hits if not h["doc"].startswith(CODE_PREFIX)]
    return hits or search(store, embedder, question, k, kinds=kinds, per_document=per_document,
                          rewritten=rewritten, dense=dense, **lexicon)


def _scope_documents(store: IndexStore, document: str, named: list[str], hits: list[dict],
                     most: int = SCOPE_DOCUMENTS) -> list[dict]:
    """The documents a broad question reads whole: `document`, else those it names, else
    those with most of its hits (see _scope_hits), up to `most` and with at least half the
    hits of the first (not code units: their elements already are whole)."""
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
           extra_sources: list[dict] = (), synonyms: list[list[str]] = (), id_normalize: str = "upper",
           context_radius: int = CONTEXT_RADIUS, where: dict | None = None, max_parts: int = MAX_PARTS,
           scope_documents: int = SCOPE_DOCUMENTS, requests_per_minute: int = 0, dry_run: bool = False,
           section: str = "") -> dict:
    """{"answer": text with [n] citations, "status": not_found | answered | partial, "uncited": the
    sentences without a citation, "consulted": how many sources it was given (see
    answer_status), "sources": those it cites [{n, cite, doc, section, file,
    kind, ref, excerpt, context}], "unused": from passages, those it does not cite [{n, cite}],
    "unsupported" and "out_of_range" (see check_citations), "scope": "passages" | "whole",
    "documents": those read whole, "requests": to the model, "plan": of reading them whole},
    plus "truncated": True if the answer is cut by max_tokens and "failed_parts" with the
    notes of the parts of whole documents that failed; with too many parts, an "error"
    instead of the answer (see answer_whole).
    scope "passages" answers from the best k passages; "whole" reads whole documents
    (`document`, or those of the question); "auto" reads them whole if the rewriting says
    the question is broad. `section` (see in_section) reads whole only that section of the
    documents, and what is below it, whatever the scope. `extra_sources` ({doc_name,
    section_title, kind, ref, context}, as DocTools.route gives them) are the first sources;
    with them the question is not rewritten (its IDs are found without it) and "auto"
    answers from passages. `where` narrows the passages, and what is read of whole
    documents, as in search.
    dry_run makes no request to any model (llm can be None): the plan of reading whole the
    documents the question would read, chosen without the model (`document`, the documents
    whose name it says, or a lexical search in the summaries), or None if there are none."""
    extra = list(extra_sources)
    if extra:
        rewrite, scope = False, "passages" if scope == "auto" else scope
    candidates = [d for d in store.documents() if not d["doc"].startswith(CODE_PREFIX)]
    if dry_run:
        named = _named(question, candidates)
        hits = [] if document or named else _scope_hits(store, embedder, question, k, kinds, per_document, None,
                                                        dense=False, where=where, synonyms=synonyms,
                                                        id_normalize=id_normalize)
        docs = _scope_documents(store, document, named, hits, scope_documents)
        allowed = store.allowed(where=where) if where else None
        plan = _with_minutes(whole_plan(store, docs, scope_chars, int(rewrite), section, extra, allowed)[2],
                             requests_per_minute) if docs else None
        return {"answer": "", "sources": [], "scope": "whole" if docs else "passages",
                "documents": [d["name"] for d in docs], "requests": 0, "plan": plan,
                "passages_requests": int(rewrite) + 1, "max_parts": max_parts}
    rewritten = rewrite_question(llm, question, candidates) if rewrite else None
    requests = 1 if rewrite else 0
    if scope == "whole" or section or (scope == "auto" and rewritten and rewritten["broad"]):
        named = rewritten["documents"] if rewritten else []
        hits = [] if document or named else _scope_hits(store, embedder, question, k, kinds, per_document,
                                                        rewritten, where=where, synonyms=synonyms,
                                                        id_normalize=id_normalize)
        docs = _scope_documents(store, document, named, hits, scope_documents)
        if docs:
            return answer_whole(store, llm, question, docs, scope_chars, max_parts, requests_per_minute, requests,
                                section, extra, where)
    hits = search(store, embedder, question, k, document, kinds, per_document=per_document, rewritten=rewritten,
                  where=where, synonyms=synonyms, id_normalize=id_normalize)
    routed = {s["ref"].upper() for s in extra if s.get("ref")}
    hits = [h for h in hits if not (h["doc"].startswith(DOORS_PREFIX) and (h.get("ref") or "").upper() in routed)]
    used = sum(len(s["context"]) for s in extra)
    sources = extra + build_context(store, hits, max(context_chars - used, context_chars // 4), context_radius)
    if not sources:
        return {**_checked(NOTHING_FOUND, [], True), "scope": "passages", "documents": [], "requests": requests}
    blocks = "\n\n".join(f"[{i}] {cite(s)}\n{s['context']}" for i, s in enumerate(sources, 1))
    text = llm.ask_text(ANSWER_SYSTEM, f"Sources:\n\n{blocks}\n\nQuestion: {question}")
    return {**_checked(str(text), sources, True), "scope": "passages", "documents": [],
            "requests": requests + _requests(text), **({"truncated": True} if _truncated(text) else {})}
