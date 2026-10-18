"""Section analysis and consolidation.

Three ways to analyse a section, chosen with `analysis` in each [[sources.docs]]:

- "llm": the model extracts summary, key points, requirements, definitions, entities,
  references and open issues as JSON. Answers are cached on disk by prompt, model and
  language, so a section whose text has not changed never goes back to the model. If the
  answer is cut by length or is not valid JSON, the section is split in two (up to 8
  pieces) and the results are merged.
- "regex": requirements only, with a regular expression and no model (standards).
- "none": structure only.

Every requirement is then looked up in the text of its section (`verify`) to catch
invented or rewritten ones, and `consolidate` gathers requirements, glossary, entities
and open issues of the whole document.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from atlas.docs.structure import tables_in
from atlas.llm.structured import JsonLlm, TruncatedResponse
from atlas.pipeline import atomic_write

__all__ = ["ANALYSES", "DEFAULT_REQ_REGEX", "PROMPT_VERSION", "add_requirements", "analyze_section", "consolidate",
           "empty_overview", "is_cached", "normalize", "overview", "overview_cache_file", "regex_requirements",
           "section_cache_file", "table_requirements", "verify"]

log = logging.getLogger(__name__)

ANALYSES = ("llm", "regex", "none")
PROMPT_VERSION = "v1"   # change it when editing the prompts: it invalidates the cache

# "Req: SRS-0001 The TMF shall...": group 1 starts with the ID
DEFAULT_REQ_REGEX = r"(?i)\bReq\s*:\s*(\S+)"

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "requirements": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["id", "text"]}},
        "definitions": {"type": "array", "items": {
            "type": "object",
            "properties": {"term": {"type": "string"}, "definition": {"type": "string"}},
            "required": ["term", "definition"]}},
        "entities": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "kind": {"type": "string"},
                           "description": {"type": "string"}},
            "required": ["name", "kind", "description"]}},
        "references": {"type": "array", "items": {"type": "string"}},
        "open_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "key_points", "requirements", "definitions",
                 "entities", "references", "open_issues"],
}

SECTION_SYSTEM = """You are a meticulous technical analyst. You receive ONE section of a \
larger document that was converted from PDF to plain text: line breaks may fall mid-sentence \
and tables may be flattened into one cell per line. Extract structured information from THIS \
section only and answer with a single JSON object.

Rules:
- Use only information present in the section. Never invent identifiers, values or requirements.
- summary: 2-4 sentences on what the section specifies or explains.
- key_points: the most important facts, values, constraints or behaviours (max 8).
- requirements: every normative statement (shall / must / will / should, or anything \
explicitly labelled as a requirement). Copy `text` VERBATIM from the section, joining broken \
lines. Put the identifier in `id` only if one appears in the text; otherwise "".
- definitions: terms, acronyms and abbreviations explicitly defined in the section.
- entities: the most relevant named systems, subsystems, functions, interfaces, messages, \
signals, parameters, equipment or documents (max 15). `kind` is one of: system, function, \
interface, message, parameter, equipment, document, other. Keep descriptions under 15 words.
- references: references to other sections, documents or standards (max 10).
- open_issues: TBD, TBC, TBA or explicitly open points, with what they refer to.
- Empty lists are fine. Never repeat an item. Output compact JSON (no indentation) and nothing \
else. {lang_rule}"""

SUMMARY_SYSTEM = """You receive the section-by-section summaries of a technical document. \
Write an overview of the whole document as a JSON object with: "summary" (one paragraph, \
5-8 sentences), "purpose" (one sentence), "scope" (one sentence) and "main_topics" \
(list of 3-10 short topics). Use only the information given. {lang_rule}"""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "purpose": {"type": "string"},
                   "scope": {"type": "string"},
                   "main_topics": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "purpose", "scope", "main_topics"],
}

MAX_SPLITS = 3   # a section can be split into up to 2^3 = 8 pieces


def lang_rule(lang: str) -> str:
    if not lang:
        return "Write in the same language as the document."
    return (f"Write summary, key_points, descriptions and overviews in {lang}; keep "
            f"requirement text, terms and definitions verbatim in the original language.")


def normalize(d: dict) -> dict:
    """Guarantees keys and types even if the model skips the schema (servers without
    structured output): an object where a text was expected becomes its values joined."""
    def as_list(x):
        return x if isinstance(x, list) else []

    def text(x) -> str:
        if isinstance(x, dict):
            return ": ".join(t for t in (text(v) for v in x.values()) if t)
        return str(x).strip() if x is not None else ""

    def texts(x) -> list[str]:
        return [t for t in (text(i) for i in as_list(x)) if t]

    def objects(x, keys):
        return [{k: text(o.get(k, "")) for k in keys} for o in as_list(x) if isinstance(o, dict)]

    return {
        "summary": text(d.get("summary", "")),
        "key_points": texts(d.get("key_points")),
        "requirements": [r for r in objects(d.get("requirements"), ["id", "text"]) if r["text"]],
        "definitions": [x for x in objects(d.get("definitions"), ["term", "definition"]) if x["term"]],
        "entities": [x for x in objects(d.get("entities"), ["name", "kind", "description"]) if x["name"]],
        "references": texts(d.get("references")),
        "open_issues": texts(d.get("open_issues")),
    }


def empty_overview() -> dict:
    return {"summary": "", "purpose": "", "scope": "", "main_topics": []}


# --------------------------------------------------------------------------
# Analysis with the model
# --------------------------------------------------------------------------

def section_prompt(s: dict, doc_title: str) -> str:
    pages = f"{s['page_start']}-{s['page_end']}" if s["page_start"] else "?"
    return (f"Document: {doc_title}\nSection path: {' > '.join(s['path'])}\n"
            f"Pages: {pages}\n\n<section>\n{s['body']}\n</section>")


def section_cache_file(cache: Path | None, model: str, s: dict, doc_title: str, lang: str) -> Path | None:
    """Cache file of the analysis of a section: it depends on the prompt (text, path and
    pages of the section, document title), the model, the language and PROMPT_VERSION."""
    if not cache:
        return None
    key = hashlib.sha256(f"{PROMPT_VERSION}|{model}|{lang}|{section_prompt(s, doc_title)}"
                         .encode("utf-8")).hexdigest()[:20]
    return Path(cache) / f"{key}.json"


def split_in_two(text: str) -> list[str]:
    """Splits at the paragraph (or line, or sentence) closest to the middle."""
    for sep in (r"\n\s*\n", r"\n", r"(?<=[.;:])\s+"):
        pieces = [x for x in re.split(sep, text) if x.strip()]
        if len(pieces) < 2:
            continue
        middle, acc = len(text) / 2, 0
        for k, piece in enumerate(pieces[:-1], 1):
            acc += len(piece)
            if acc >= middle:
                break
        joiner = "\n\n" if sep.startswith(r"\n\s") else ("\n" if sep == r"\n" else " ")
        return [joiner.join(pieces[:k]), joiner.join(pieces[k:])]
    return [text]


def merge_analyses(parts: list[dict]) -> dict:
    """Joins the analyses of the pieces of a split section. Requirements are deduplicated
    by ID, or by text if they have none (as in `consolidate`)."""
    out = normalize({})
    out["summary"] = " ".join(p["summary"] for p in parts if p["summary"])
    out["key_points"] = [k for p in parts for k in p["key_points"]][:10]
    keys = {"requirements": lambda r: r["id"].upper() or r["text"].lower(),
            "definitions": lambda d: d["term"].lower(), "entities": lambda e: e["name"].lower()}
    for field, key in keys.items():
        seen = set()
        for p in parts:
            for x in p[field]:
                if key(x) not in seen:
                    seen.add(key(x))
                    out[field].append(x)
    for field in ("references", "open_issues"):
        out[field] = list(dict.fromkeys(x for p in parts for x in p[field]))
    return out


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _analyze_text(llm: JsonLlm, s: dict, body: str, doc_title: str, lang: str,
                  failures: Path | None, level: int = 0, suffix: str = "") -> dict:
    sub = dict(s, body=body, path=s["path"] + ([f"(part {suffix.lstrip('.')})"] if suffix else []))
    system = SECTION_SYSTEM.replace("{lang_rule}", lang_rule(lang))

    def keep(attempt: int, text: str, finish: str) -> None:
        if failures:
            failures.mkdir(parents=True, exist_ok=True)
            (failures / f"{s['id']}{suffix}_attempt{attempt}.txt").write_text(
                f"# section {s['id']}{suffix} {s['title']} | finish_reason={finish or '?'} | "
                f"{len(text)} chars | {_now()}\n\n{text}", encoding="utf-8")

    try:
        return normalize(llm.ask_json(system, section_prompt(sub, doc_title), SECTION_SCHEMA, "section", keep))
    except (TruncatedResponse, RuntimeError) as e:
        halves = split_in_two(body) if level < MAX_SPLITS and len(body) > 1500 else [body]
        if len(halves) < 2:
            if isinstance(e, TruncatedResponse) and e.partial is not None:
                log.warning("%s%s: answer cut and it cannot be split further; keeping what was recovered",
                            s["id"], suffix)
                return normalize(e.partial)
            raise
        reason = "answer cut by length" if isinstance(e, TruncatedResponse) else "invalid JSON"
        log.info("%s%s: %s; splitting it in 2 parts (%d chars)", s["id"], suffix, reason, len(body))
        return merge_analyses([
            _analyze_text(llm, s, half, doc_title, lang, failures, level + 1, f"{suffix}.{k}")
            for k, half in enumerate(halves, 1)])


def analyze_section(llm: JsonLlm, s: dict, doc_title: str, lang: str = "", cache: Path | None = None,
                    failures: Path | None = None, shared=None) -> dict:
    """Analysis of a section with the model, from the cache if it is there (or from the
    shared cache, atlas.cache.SharedCache, which is only read). `failures` is a folder
    where the invalid answers of the model are kept for diagnosis."""
    file = section_cache_file(cache, llm.model, s, doc_title, lang)
    cached = _read_cache(file, shared)
    if cached is not None:
        return cached
    result = _analyze_text(llm, s, s["body"], doc_title, lang, failures)
    if file:
        atomic_write(file, json.dumps(result, ensure_ascii=False, indent=1))
    return result


def _read_cache(file: Path | None, shared=None) -> dict | None:
    """A cached analysis, or None if there is none. A damaged file (written by a version
    that did not write atomically and was cut) counts as missing. Without a local file,
    the one of the same name in the shared cache, if any, which is copied to the local
    cache (the shared one is never written)."""
    if not file:
        return None
    if not file.exists():
        content = shared.analysis(file.name) if shared is not None else None
        if content is None:
            return None
        try:
            data = json.loads(content)
        except ValueError:
            return None
        atomic_write(file, content)
        return data
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log.warning("damaged cache file %s, analysed again: %s", file.name, e)
        return None


def _overview_items(sections: list[dict]) -> list[str]:
    return [f"[{s['title']}] {s['analysis']['summary']}" for s in sections if s["analysis"]["summary"]]


def overview_cache_file(cache: Path | None, model: str, sections: list[dict], doc_title: str,
                        lang: str) -> Path | None:
    """Cache file of the overview of a document: it depends on the summaries of its
    sections (so they must be analysed), the title, the model, the language and
    PROMPT_VERSION."""
    if not cache:
        return None
    key = hashlib.sha256(f"{PROMPT_VERSION}|{model}|{lang}|{doc_title}|{_overview_items(sections)}"
                         .encode("utf-8")).hexdigest()[:20]
    return Path(cache) / f"overview_{key}.json"
def is_cached(file: Path | None, shared=None) -> bool:
    """Whether the analysis of `file` is in the local cache or in the shared one."""
    return bool(file) and (file.exists() or (shared is not None and shared.has_analysis(file.name)))


def overview(llm: JsonLlm, sections: list[dict], doc_title: str, lang: str = "",
             max_chars: int = 12000, cache: Path | None = None, shared=None, sent: list | None = None) -> dict:
    """Overview of the whole document from the section summaries. Hierarchical: if the
    summaries do not fit in one request, they are summarised in groups first. `sent`, a
    list, gets an item when the model is asked (not from the cache)."""
    items = _overview_items(sections)
    file = overview_cache_file(cache, llm.model, sections, doc_title, lang)
    cached = _read_cache(file, shared)
    if cached is not None:
        return cached
    if sent is not None and items:
        sent.append(doc_title)
    result = _overview(llm, items, doc_title, lang, max_chars)
    if file:
        atomic_write(file, json.dumps(result, ensure_ascii=False, indent=1))
    return result


def _overview(llm: JsonLlm, items: list[str], doc_title: str, lang: str, max_chars: int) -> dict:
    system = SUMMARY_SYSTEM.replace("{lang_rule}", lang_rule(lang))
    while len("\n".join(items)) > max_chars and len(items) > 1:
        groups, buf = [], []
        for it in items:
            if buf and len("\n".join(buf + [it])) > max_chars:
                groups.append(buf)
                buf = []
            buf.append(it)
        groups.append(buf)
        items = [llm.ask_json(system, f"Document: {doc_title}\n\n" + "\n".join(g),
                              SUMMARY_SCHEMA, "overview").get("summary", "") for g in groups]
    if not items:
        return empty_overview()
    d = llm.ask_json(system, f"Document: {doc_title}\n\n" + "\n".join(items), SUMMARY_SCHEMA, "overview")
    return {"summary": str(d.get("summary", "")), "purpose": str(d.get("purpose", "")),
            "scope": str(d.get("scope", "")),
            "main_topics": [str(x) for x in d.get("main_topics", []) if x]}


# --------------------------------------------------------------------------
# Analysis with a regular expression
# --------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s*$")


# What may come between the IDs of one statement: "SRS-0004, SRS-0005", "SRS-0004 / SRS-0005",
# "SRS-0004 and SRS-0005", "[SRS-0002]:"
_CLOSING = re.compile(r"[\])}]*")
_BETWEEN_IDS = re.compile(r"(?:[\s,;/&:()\[\]]|\b(?:and|or|y|o)\b)*", re.IGNORECASE)


def _leading_ids(body: str, start: int, ids: re.Pattern) -> tuple[list[str], int]:
    """The IDs written one after the other from `start` (only separators between them), and
    where the text after them starts. None at the start: the first ID of the rest of the
    line after a word or two ("Req: see SRS-0004"), not one that the statement cites ("Req:
    The TMF shall log as SRS-0100 says")."""
    found, pos = [], start
    while True:
        gap = _BETWEEN_IDS.match(body, pos)
        m = ids.match(body, gap.end())
        if not m or m.end() == m.start():
            break
        found.append(m.group(0))
        pos = m.end()
    if not found:
        line_end = body.find("\n", start)
        m = ids.search(body, start, len(body) if line_end < 0 else line_end)
        if m and len(body[start:m.start()].split()) <= 2:
            found, pos = [m.group(0)], m.end()
    return found, _CLOSING.match(body, pos).end()      # "[SRS-0002]: The TMF..." -> ": The TMF..."


def regex_requirements(body: str, pattern: re.Pattern | str, ids: re.Pattern | str | None = None) -> list[dict]:
    """Requirements marked by a regular expression, without a model.

    Each match starts a requirement. Its ID is the first word of group 1 (without
    brackets or punctuation); without groups, it has no ID and the text starts at the
    match. With `ids` (the expression of an ID, from [sources.doors.convention]), its IDs
    are the ones written one after the other from group 1 ("Req: SRS-0004 and SRS-0005 The
    TMF shall..." gives the same requirement twice, one per ID), or the first one of group 1
    if it does not start with one. The text runs from the IDs to the next match, a blank
    line or the end of a line that closes a sentence, whichever comes first, with the lines
    joined. A blank line does not end an unfinished sentence that goes on in lower case
    after it (a page break in the middle of the sentence)."""
    pattern = re.compile(pattern) if isinstance(pattern, str) else pattern
    ids = re.compile(ids) if isinstance(ids, str) and ids else (ids or None)
    matches = list(pattern.finditer(body))
    out = []
    for k, m in enumerate(matches):
        rids = [""]
        if pattern.groups and ids is not None and m.group(1) is not None:
            found, start = _leading_ids(body, m.start(1), ids)
            if found:
                rids = list(dict.fromkeys(found))
            else:
                start = m.start(1)
        elif pattern.groups and (m.group(1) or "").split():
            token = m.group(1).split()[0]
            rids = [token.strip(".,;:()[]")]
            start = m.start(1) + m.group(1).find(token) + len(token)
        else:
            start = m.start()
        end = matches[k + 1].start() if k + 1 < len(matches) else len(body)
        lines: list[str] = []
        chunk = body[start:end].split("\n")
        i = 0
        while i < len(chunk):
            if not chunk[i].strip():
                j = i
                while j < len(chunk) and not chunk[j].strip():
                    j += 1
                if lines and not (j < len(chunk) and chunk[j].strip()[0].islower()):
                    break
                i = j
                continue
            lines.append(chunk[i].strip())
            if _SENTENCE_END.search(chunk[i]):
                break
            i += 1
        text = " ".join(lines).lstrip(" -–—:")
        if text:
            out.extend({"id": rid, "text": text} for rid in rids)
    return out


def table_requirements(body: str, columns: dict) -> list[dict]:
    """One requirement per row of each Markdown table of the section whose header has the
    columns of `columns` ({"id": "ID", "text": "Requirement"}, any case): the ID is the
    first word of its cell, the text the cell of `text`. Rows without text are skipped."""
    want_id, want_text = (str(columns.get(k, "")).strip().lower() for k in ("id", "text"))
    out = []
    for t in tables_in(body):
        header = [h.lower() for h in t["header"]]
        if want_id not in header or want_text not in header:
            continue
        i, j = header.index(want_id), header.index(want_text)
        for row in t["rows"]:
            text = " ".join(row[j].split()) if j < len(row) else ""
            rid = row[i].split()[0].strip(".,;:()[]") if i < len(row) and row[i].split() else ""
            if text:
                out.append({"id": rid, "text": text})
    return out


def add_requirements(analysis: dict, extra: list[dict]) -> dict:
    """`analysis` with the requirements of `extra` (the rows of the tables) that it does not
    have: one whose ID it has is skipped, one whose text it has without an ID gives it that
    ID, and the rest are added."""
    reqs = [dict(r) for r in analysis["requirements"]]
    ids = {r["id"].upper() for r in reqs if r["id"]}
    without_id = {_canon(r["text"]): r for r in reqs if not r["id"]}
    for r in extra:
        if r["id"] and r["id"].upper() in ids:
            continue
        same = without_id.pop(_canon(r["text"]), None)
        if same is not None:
            same["id"] = r["id"]
        elif r["id"] or _canon(r["text"]) not in {_canon(x["text"]) for x in reqs}:
            reqs.append(dict(r))
        ids.add(r["id"].upper())
    return {**analysis, "requirements": reqs}


# --------------------------------------------------------------------------
# Verification and consolidation
# --------------------------------------------------------------------------

def _canon(s: str) -> str:
    return re.sub(r"\W+", " ", s.replace("-\n", "")).lower().strip()


def verify(req_text: str, source: str) -> str:
    """literal | partial | not_found — detects invented or rewritten requirements."""
    a, b = _canon(req_text), _canon(source)
    if a and a in b:
        return "literal"
    words = set(a.split())
    if words and len(words & set(b.split())) / len(words) >= 0.85:
        return "partial"
    return "not_found"


def consolidate(sections: list[dict]) -> dict:
    """Requirements (verified against their section), glossary, entities and open issues
    of the whole document. Requirements are deduplicated by ID, or by text if they have
    none."""
    glossary: dict[str, dict] = {}
    entities: dict[str, dict] = {}
    requirements, open_issues, seen = [], [], set()
    for s in sections:
        a = s["analysis"]
        for r in a["requirements"]:
            key = r["id"].upper() or _canon(r["text"])
            if key in seen:
                continue
            seen.add(key)
            r["status"] = verify(r["text"], s["body"])
            requirements.append({**r, "section": s["id"], "section_title": s["title"]})
        for d in a["definitions"]:
            g = glossary.setdefault(d["term"].lower(), {"term": d["term"], "definitions": [], "sections": []})
            if d["definition"] and d["definition"] not in g["definitions"]:
                g["definitions"].append(d["definition"])
            if s["id"] not in g["sections"]:
                g["sections"].append(s["id"])
        for e in a["entities"]:
            x = entities.setdefault(e["name"].lower(), {"name": e["name"], "kinds": Counter(),
                                                         "description": "", "sections": []})
            x["kinds"][e["kind"].lower() or "other"] += 1
            if not x["description"] and e["description"]:
                x["description"] = e["description"]
            if s["id"] not in x["sections"]:
                x["sections"].append(s["id"])
        for p in a["open_issues"]:
            open_issues.append({"text": p, "section": s["id"], "section_title": s["title"]})
    for x in entities.values():
        x["kind"] = x.pop("kinds").most_common(1)[0][0]
    return {"glossary": sorted(glossary.values(), key=lambda g: g["term"].lower()),
            "entities": sorted(entities.values(), key=lambda e: (e["kind"], e["name"].lower())),
            "requirements": requirements, "open_issues": open_issues}
