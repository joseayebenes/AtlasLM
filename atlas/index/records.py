"""Search records of a document, built from its structure.json (atlas.docs), and of a
DOORS module, built from its requirements (atlas.doors).

Each document is indexed at several granularities, each one a `kind`:

    overview          the overview of the whole document
    section_summary   summary and key points of each section
    chunk             ~chunk_chars pieces of the text of each section, with overlap
    requirement       each requirement, with its ID in `ref`
    definition        each glossary term, with the term in `ref`
    open_issue        each TBD / TBC

`text` is what is shown and `embed_text` what is embedded and searched: it carries the
document title and the section path, and each chunk also the summary of its section
(contextual retrieval), so that a loose paragraph is still found by what it is about.

A DOORS module is indexed as one more document: each requirement is a `requirement` record
and each information object a `chunk`, with the headings above it as its path.

A code unit (an Ada package: its spec and its body) is one more document too: each
subprogram, test, type, variable and constant is a `code_doc` record, with its qualified
name (`tracks.delete_track`) in `ref` and, as text, its header comment, the requirements it
cites and its code.

Each record also has `meta`, what a search can be narrowed by (keys in lower case, values
as text): in a DOORS module, `module` and the attributes of [sources.doors] attributes
(`status`, `verification` by default), which also go in its text, so that they weigh in the
search too; `chapter`, the first heading of its path, in every record that has one; in a
code unit, `kind` (subprogram | test | type | variable | constant) and `file`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

__all__ = ["ATTRIBUTES", "CODE_PREFIX", "DOORS_PREFIX", "KINDS", "build_records", "chunk_text", "code_records",
           "reflow", "requirement_records"]

DOORS_PREFIX = "doors:"     # key of a DOORS module in the index: "doors:SRS_TMS"
CODE_PREFIX = "code:"       # key of a code unit in the index: "code:tracks"
KINDS = ("overview", "section_summary", "chunk", "requirement", "definition", "open_issue", "code_doc")
ATTRIBUTES = ("status", "verification")     # [sources.doors] attributes by default


def _chapter(path: list[str] | str) -> dict:
    """The meta of a record with that path: its first element, the chapter."""
    first = (path.split(" > ")[0] if isinstance(path, str) else (path or [""])[0]).strip()
    return {"chapter": first} if first else {}


def reflow(text: str) -> str:
    """Joins the lines broken by the PDF inside each paragraph."""
    pars = [" ".join(l.strip() for l in p.split("\n") if l.strip()) for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(p for p in pars if p)


def chunk_text(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    """Pieces of ~size characters by paragraphs and sentences, with overlap."""
    units = []
    for par in reflow(text).split("\n\n"):
        if len(par) <= size:
            units.append(par)
        else:
            units.extend(re.split(r"(?<=[.;:!?])\s+", par))
    chunks, buf = [], ""
    for u in units:
        while len(u) > size:                       # huge sentence (flattened table)
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(u[:size])
            u = u[size - overlap:]
        if buf and len(buf) + len(u) + 1 > size:
            chunks.append(buf)
            tail = buf[-overlap:]
            m = re.search(r"[.;:!?]\s+", tail)          # the overlap starts at a whole sentence
            buf = tail[m.end():] if m else (tail[tail.find(" ") + 1:] if " " in tail else "")
        buf = f"{buf} {u}".strip() if buf else u
    if buf:
        chunks.append(buf)
    return chunks


def build_records(data: dict, chunk_chars: int = 1200, chunk_overlap: int = 200,
                  kinds: Iterable[str] | None = None) -> list[dict]:
    """Records of a document from its structure.json; `kinds` limits which ones."""
    title = data["title"]
    records = []
    ov = data.get("overview") or {}
    if ov.get("summary"):
        text = "\n".join(x for x in [ov.get("summary", ""), f"Purpose: {ov.get('purpose', '')}",
                                     f"Scope: {ov.get('scope', '')}",
                                     "Topics: " + ", ".join(ov.get("main_topics", []))] if x)
        records.append({"kind": "overview", "text": text, "embed_text": f"{title} — document overview\n{text}"})

    for s in data["sections"]:
        a = s.get("analysis") or {}
        path = " > ".join(s.get("path") or [s["title"]])
        base = {"section_id": s["id"], "section_title": s["title"], "path": path,
                "page_start": s.get("page_start") or 0, "page_end": s.get("page_end") or 0,
                "meta": _chapter(s.get("path") or [s["title"]])}
        header = f"{title} > {path}"

        if a.get("summary"):
            text = a["summary"] + ("\n- " + "\n- ".join(a["key_points"]) if a.get("key_points") else "")
            records.append({**base, "kind": "section_summary", "text": text, "embed_text": f"{header}\n{text}"})

        # contextual retrieval: each chunk is embedded with its path and its section summary
        context = f"{header}\n{a.get('summary', '')}".strip()
        for k, ch in enumerate(chunk_text(s.get("body", ""), chunk_chars, chunk_overlap)):
            records.append({**base, "kind": "chunk", "seq": k, "text": ch, "embed_text": f"{context}\n\n{ch}"})

    for r in data.get("requirements", []):
        sec = next((s for s in data["sections"] if s["id"] == r["section"]), {})
        text = (f"{r['id']}: " if r["id"] else "") + r["text"]
        records.append({"kind": "requirement", "ref": r["id"], "text": text,
                        "section_id": r["section"], "section_title": r.get("section_title", ""),
                        "path": " > ".join(sec.get("path", [])),
                        "page_start": sec.get("page_start") or 0, "page_end": sec.get("page_end") or 0,
                        "meta": _chapter(sec.get("path", [])),
                        "embed_text": f"{title} > requirement\n{text}"})
    titles = {s["id"]: s["title"] for s in data["sections"]}
    for g in data.get("glossary", []):
        text = f"{g['term']}: {' / '.join(g['definitions'][:3])}"
        sid = (g.get("sections") or [""])[0]
        records.append({"kind": "definition", "ref": g["term"], "text": text,
                        "section_id": sid, "section_title": titles.get(sid, ""),
                        "embed_text": f"{title} > glossary\n{text}"})
    for p in data.get("open_issues", []):
        records.append({"kind": "open_issue", "text": p["text"], "section_id": p["section"],
                        "section_title": p.get("section_title", ""),
                        "embed_text": f"{title} > open issue (TBD)\n{p['text']}"})
    if kinds is not None:
        wanted = set(kinds)
        records = [r for r in records if r["kind"] in wanted]
    return records


def requirement_records(module: str, requirements: list[dict], kinds: Iterable[str] | None = None,
                        attributes: Iterable[str] = ATTRIBUTES) -> list[dict]:
    """Records of a DOORS module from its rows in atlas.db (Store.requirements): one
    `requirement` per requirement, with its ID in `ref`, and one `chunk` per information
    object. The headings above each one are its path; the nearest, its section. The
    `attributes` a requirement has (by name, in any case) go in its meta and, as a line
    "status: Approved; verification: Test", in its text."""
    by_id = {r["id"]: r for r in requirements}
    attributes = list(dict.fromkeys(attributes))

    def shown(r: dict) -> list[tuple[str, str]]:
        values = {str(k).lower(): v for k, v in (r.get("attributes") or {}).items()}
        return [(a, str(values[a.lower()]).strip()) for a in attributes
                if values.get(a.lower()) is not None and str(values[a.lower()]).strip()]
    kind_of = lambda r: (r.get("attributes") or {}).get("kind", "requirement")

    def title(r: dict) -> str:
        a = r.get("attributes") or {}
        return " ".join(x for x in (str(a.get("outline") or ""), a.get("heading") or r["text"]) if x).strip()

    records = []
    for r in requirements:
        if kind_of(r) == "heading" or not r["text"]:
            continue
        above, parent, seen = [], r.get("parent"), set()
        while parent and parent in by_id and parent not in seen:
            seen.add(parent)
            if kind_of(by_id[parent]) == "heading":
                above.append(by_id[parent])
            parent = by_id[parent].get("parent")
        above.reverse()
        path = " > ".join(title(h) for h in above)
        base = {"section_id": above[-1]["id"] if above else "", "section_title": title(above[-1]) if above else "",
                "path": path, "meta": {"module": module, **_chapter([title(h) for h in above])}}
        header = " > ".join(x for x in (module, path) if x)
        if kind_of(r) == "information":
            records.append({**base, "kind": "chunk", "seq": 0, "text": r["text"],
                            "embed_text": f"{header}\n\n{r['text']}"})
        else:
            values = shown(r)
            text = f"{r['id']}: {r['text']}" + ("\n" + "; ".join(f"{a}: {v}" for a, v in values) if values else "")
            records.append({**base, "kind": "requirement", "ref": r["id"], "text": text,
                            "meta": {**{a.lower(): v for a, v in values}, **base["meta"]},
                            "embed_text": f"{header} > requirement\n{text}"})
    if kinds is not None:
        wanted = set(kinds)
        records = [r for r in records if r["kind"] in wanted]
    return records


def code_records(unit: str, parts: list[dict], sources: dict[str, str], tests: Iterable[str] = (),
                 chunk_chars: int = 1200, kinds: Iterable[str] | None = None) -> list[dict]:
    """Records of a code unit from its files as code.parse leaves them in atlas.db (CodeUnit
    as a dict; an Ada package has its spec and its body): one `code_doc` per subprogram,
    test, type, variable and constant. The comment of a subprogram is the one of its
    declaration in the spec, if there is one; its code, the body (up to chunk_chars
    characters). `sources` has the text of each file and `tests` the files that are tests."""
    if kinds is not None and "code_doc" not in set(kinds):
        return []
    tests = set(tests)
    elements: dict[str, dict] = {}

    def element(name: str, kind: str) -> dict:
        return elements.setdefault(name, {"name": name, "kind": kind, "doc": "", "reqs": [], "where": "", "code": ""})

    def lines(file: str, start: int, end: int) -> str:
        return "\n".join(sources.get(file, "").splitlines()[start - 1:end]) if start else ""

    def merge(e: dict, item: dict) -> None:
        e["doc"] = e["doc"] or item.get("doc", "")
        e["reqs"] += [r for r in item.get("reqs", []) if r not in e["reqs"]]

    for part in sorted(parts, key=lambda p: (not p.get("declarations"), p["file"])):    # specs first
        file, name = part["file"], Path(part["file"]).name
        for d in part.get("declarations", []):
            e = element(d["name"], "subprogram")
            merge(e, d)
            e["where"] = e["where"] or f"{name}:{d['line']}"
            e["code"] = e["code"] or lines(file, d["line"], d["line"])
        for sp in part.get("subprograms", []):
            e = element(sp["name"], "subprogram")
            e["kind"] = "test" if file in tests else e["kind"]
            merge(e, sp)
            e["where"] = f"{name}:{sp['start_line']}-{sp['end_line']}"
            e["code"] = lines(file, sp["start_line"], sp["end_line"]) or e["code"]
        for t in part.get("types", []):
            e = element(t["name"], "type")
            merge(e, t)
            e["where"], e["code"] = f"{name}:{t['line']}", t.get("source", "")
        for v in part.get("variables", []):
            e = element(v["name"], "constant" if v.get("constant") else "variable")
            e["where"], e["code"] = f"{name}:{v['line']}", v.get("source", "")

    language = parts[0].get("language", "") if parts else ""
    records = []
    for n, e in enumerate(elements.values()):
        ref = f"{unit}.{e['name']}"
        code = e["code"].strip("\n")
        if len(code) > chunk_chars:
            code = code[:chunk_chars].rstrip() + "\n…"
        unsaid = [r for r in e["reqs"] if r not in e["doc"] and r not in code]      # the Req: tag usually says them
        body = "\n".join(x for x in (e["doc"], f"Requirements: {', '.join(unsaid)}" if unsaid else "", code) if x)
        records.append({"kind": "code_doc", "ref": ref, "section_id": ref, "section_title": f"{ref} · {e['where']}",
                        "path": unit, "seq": n, "meta": {"kind": e["kind"], "file": e["where"].split(":")[0]},
                        "text": f"{ref} ({e['kind']}, {e['where']})\n{body}",
                        "embed_text": f"{language} unit {unit} > {e['kind']} {ref}\n{body}"})
    return records
