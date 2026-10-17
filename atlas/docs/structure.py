"""Document structure without a model: header and footer cleaning, sections from the PDF
bookmarks, from the headings of the text or in fixed-size blocks, and the heading
diagnosis. Comes from section 2 of legacy/pdf_struct.py.

A section is a dict with `level`, `title`, `path` (list of titles from the chapter down),
`body`, `page_start` and `page_end`; `prepare_sections` adds `id` and `includes` (titles
of the short sections merged into it). Pages follow the page breaks (\\f) of the text.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

__all__ = ["METADATA_KEYS", "STRUCTURES", "clean_headers", "detect_sections", "diagnose_headings",
           "document_metadata", "document_title", "prepare_sections", "sections_from_bookmarks", "split_text"]

STRUCTURES = ("auto", "bookmarks", "headings", "size")

MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# "2", "2.1", "3.4.1", "A.1", "B.2.3" + a title that starts with a capital letter. Table of
# contents entries ("1 Intro ..... 4") and normative sentences are discarded.
NUM_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,5}|[A-Z](?:\.\d{1,3}){1,5})\.?\s{1,4}"
                         r"([A-ZÁÉÍÓÚÑ][^\n]{1,100})$")
ANNEX = re.compile(r"^((?:ANNEX|APPENDIX|ANEXO|APÉNDICE|Annex|Appendix|Anexo|Apéndice)\s+[A-Z0-9]{1,3})"
                   r"\b[\s.:–-]*(.{0,100})$")
NUMBER_ONLY = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,5}|[A-Z](?:\.\d{1,3}){1,5})\.?$")
TOC_LINE = re.compile(r"(\.{3,}|…)\s*\d+\s*$")
NORMATIVE = re.compile(r"\b(shall|must|should)\b", re.I)

PREAMBLE = "Preamble"


def _rejection(title: str, line: str) -> str | None:
    if TOC_LINE.search(line):
        return "table of contents entry"
    if title.endswith((".", ",", ";", ":")):
        return "ends with punctuation"
    if len(title.split()) > 12:
        return "more than 12 words"
    if NORMATIVE.search(title):
        return "normative sentence (shall/must)"
    return None


def numbered_heading(line: str, pattern: re.Pattern) -> tuple[int, str] | None:
    """(level, title) if the line is a numbered heading (or an annex), else None."""
    l = line.strip()
    m = ANNEX.match(l) if pattern is NUM_HEADING else None
    if m and not TOC_LINE.search(l):
        return 1, f"{m.group(1)} {m.group(2)}".strip()
    m = pattern.match(l)
    if not m:
        return None
    number, title = m.group(1), m.group(2).strip()
    if _rejection(title, line):
        return None
    return number.count(".") + 1, f"{number} {title}"


def clean_headers(text: str) -> tuple[str, int]:
    """Removes repeated page headers and footers (short lines at the start or the end of
    many pages, compared with the digits replaced by #). Besides cleaning the text, it
    keeps a page number from being taken as a heading. Returns (text, lines removed).

    A line that is just as frequent in the middle of the pages is body text that follows a
    template ("Req: SRS-0012 The TMF shall..."), not a header, even if it falls at the edge
    of some pages: it is kept."""
    pages = text.split("\f")
    repeated = _repeated(pages)
    if not repeated:
        return text, 0
    removed, new_pages = 0, []
    for page in pages:
        lines = page.split("\n")
        edges = _edges(lines)
        keep = []
        for i, l in enumerate(lines):
            if i in edges and len(l.strip()) <= 120 and _norm(l) in repeated:
                removed += 1
                continue
            keep.append(l)
        new_pages.append("\n".join(keep))
    return "\f".join(new_pages), removed


def _norm(line: str) -> str:
    return re.sub(r"\d+", "#", " ".join(line.split()).lower())


def _edges(lines: list[str]) -> set[int]:
    """The positions of the first and last three lines with text of a page."""
    idx = [i for i, l in enumerate(lines) if l.strip()]
    return set(idx[:3] + idx[-3:])


def _repeated(pages: list[str]) -> set[str]:
    """The page headers and footers of clean_headers, normalised (_norm)."""
    if len(pages) < 4:
        return set()
    at_edges: Counter = Counter()
    in_body: Counter = Counter()
    for page in pages:
        lines = [l for l in page.split("\n") if l.strip()]
        for l in set(_norm(x) for x in lines[:3] + lines[-3:] if len(x.strip()) <= 120):
            at_edges[l] += 1
        in_body.update(_norm(x) for x in lines[3:-3])
    threshold = max(3, int(len(pages) * 0.3))
    return {l for l, n in at_edges.items() if n >= threshold and in_body[l] < n}


METADATA_KEYS = ("doc_id", "version", "date")


def document_metadata(text: str, pdf_metadata: dict | None = None, doc_id_regex: str = "",
                      version_regex: str = "", date_regex: str = "") -> dict[str, str]:
    """The code, version and date of a document: each regular expression (group 1 if it
    has one, else the whole match) is looked for in its repeated page headers and footers
    first, then in the first two pages, then in the title, subject and keywords of the PDF
    metadata. The text is the extracted one, before clean_headers removes the headers.
    Without a date found, the modification (or creation) date of the PDF. A key that is not
    found is ""."""
    pages = text.split("\f")
    repeated = _repeated(pages)
    headers: dict[str, None] = {}          # in order, without repetitions
    for page in pages:
        lines = page.split("\n")
        headers.update((" ".join(lines[i].split()), None) for i in sorted(_edges(lines))
                       if _norm(lines[i]) in repeated)
    meta = pdf_metadata or {}
    places = ["\n".join(headers), "\n".join(pages[:2]),
              "\n".join(str(meta.get(k) or "") for k in ("title", "subject", "keywords"))]
    out = {}
    for key, regex in zip(METADATA_KEYS, (doc_id_regex, version_regex, date_regex)):
        out[key] = ""
        if regex:
            pattern = re.compile(regex)
            for place in places:
                m = pattern.search(place)
                if m:
                    out[key] = " ".join((m.group(1) if pattern.groups else m.group(0)).split())
                    break
    if not out["date"]:
        for k in ("modDate", "creationDate"):
            m = re.match(r"(?:D:)?(\d{4})(\d{2})(\d{2})", str(meta.get(k) or ""))
            if m:
                out["date"] = "-".join(m.groups())
                break
    return out


def _looks_like_title(l: str) -> bool:
    l = l.replace("\f", "").strip()
    return bool(l) and l[0].isupper() and not NUMBER_ONLY.match(l) and _rejection(l, l) is None


def _join_loose_numbers(lines: list[str]) -> list[str]:
    """pdfminer often splits the number from the title ("3.2.1" on one line and "Track
    Management" on the next) and even groups several numbers and then their titles
    ("1.1", "1.1.1", "Function group", "Track handling"). They are paired in order."""
    out, i, n = [], 0, len(lines)
    while i < n:
        if not NUMBER_ONLY.match(lines[i].replace("\f", "").strip()):
            out.append(lines[i])
            i += 1
            continue
        # run of loose numbers (with blank lines in between)
        nums, j = [], i
        while j < n and (NUMBER_ONLY.match(lines[j].replace("\f", "").strip()) or not lines[j].strip()):
            if lines[j].strip():
                nums.append(j)
            j += 1
        # following run of titles, as many as numbers
        titles, k = [], j
        while k < n and len(titles) < len(nums) and (_looks_like_title(lines[k]) or not lines[k].strip()):
            if lines[k].strip():
                titles.append(k)
            k += 1
        if not titles:
            out.extend(lines[i:j])
            i = j
            continue
        pairs = min(len(nums), len(titles))
        breaks = "\f" * sum(lines[x].count("\f") for x in range(i, k))
        for a, b in zip(nums[:pairs], titles[:pairs]):
            num = lines[a].replace("\f", "").strip().rstrip(".")
            out.append((breaks if a == nums[0] else "") + f"{num} {lines[b].replace(chr(12), '').strip()}")
        out.extend(lines[x] for x in nums[pairs:])   # numbers without a title: left as they are
        i = titles[pairs - 1] + 1
    return out


def _paths(sections: list[dict]) -> None:
    stack: list[tuple[int, str]] = []
    for s in sections:
        while stack and stack[-1][0] >= s["level"] > 0:
            stack.pop()
        s["path"] = [t for _, t in stack] + [s["title"]]
        if s["level"] > 0:
            stack.append((s["level"], s["title"]))


def detect_sections(text: str, heading_regex: str | None = None, first_page: int = 1) -> list[dict]:
    """Sections by the headings of the text (Markdown # or numbered), with level, title,
    path, body and pages."""
    num_pattern = re.compile(heading_regex) if heading_regex else NUM_HEADING
    lines = text.split("\n")
    markdown = sum(1 for l in lines if MD_HEADING.match(l)) >= 2
    if not markdown and not heading_regex:
        lines = _join_loose_numbers(lines)

    sections = []
    current = {"level": 0, "title": PREAMBLE, "lines": [], "page_start": first_page, "page_end": first_page}
    page = first_page
    for line in lines:
        page += line.count("\f")
        clean = line.replace("\f", "")
        heading = None
        if markdown:
            m = MD_HEADING.match(clean)
            if m:
                heading = (len(m.group(1)), m.group(2).strip())
        else:
            heading = numbered_heading(clean, num_pattern)
        if heading:
            sections.append(current)
            current = {"level": heading[0], "title": heading[1], "lines": [],
                       "page_start": page, "page_end": page}
        else:
            current["lines"].append(clean)
            if clean.strip():
                current["page_end"] = page
    sections.append(current)
    _paths(sections)
    for s in sections:
        s["body"] = "\n".join(s.pop("lines")).strip()
    return [s for s in sections if s["body"] or s["level"] > 0]


def sections_from_bookmarks(pdf: Path, text: str) -> tuple[list[dict], int, int] | None:
    """Sections from the bookmarks (navigable outline) of the PDF itself, the most reliable
    source of structure. Returns (sections, bookmarks, found in the text), or None if the
    PDF has fewer than 3 bookmarks or pymupdf is not installed."""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            return None
    try:
        with pymupdf.open(str(pdf)) as doc:
            toc = [t for t in doc.get_toc(simple=True) if t[2] >= 1 and str(t[1]).strip()]
    except Exception:
        return None
    if len(toc) < 3:
        return None

    starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\f"]
    page_of = lambda pos: sum(1 for x in starts if x <= pos)

    marks, prev, found = [], 0, 0
    for level, title, page in toc:
        title = " ".join(str(title).split())
        a = starts[min(page, len(starts)) - 1]
        b = starts[page + 1] if page + 1 < len(starts) else len(text)
        since = max(a, prev)
        match = None
        words = re.findall(r"\w+", title)
        for attempt in (words, words[1:] if words and re.match(r"[\d.]+$", words[0]) else []):
            if not attempt or since >= b:
                continue
            m = re.compile(r"\b" + r"\W+".join(map(re.escape, attempt)), re.I).search(text, since, b)
            if m:
                match = m
                break
        if match:
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            pos, body_from = max(line_start, prev), (line_end if line_end >= 0 else len(text))
            found += 1
        else:
            pos = body_from = since
        marks.append((pos, body_from, int(level), title))
        prev = max(prev, body_from)

    sections = [{"level": 0, "title": PREAMBLE, "page_start": 1, "page_end": page_of(marks[0][0]),
                 "body": text[:marks[0][0]]}]
    for k, (pos, body_from, level, title) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(text)
        body_from = min(body_from, end)
        sections.append({"level": level, "title": title, "page_start": page_of(pos),
                         "page_end": page_of(max(pos, end - 1)), "body": text[body_from:end]})
    _paths(sections)
    for s in sections:
        s["body"] = s["body"].replace("\f", "\n").strip()
    return [s for s in sections if s["body"] or s["level"] > 0], len(toc), found


def diagnose_headings(text: str) -> dict[str, list[str]]:
    """Lines that start like a numbered heading, grouped by why they are accepted or not."""
    groups: dict[str, list[str]] = {}
    lines = _join_loose_numbers(text.split("\n"))
    pattern = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,5}|[A-Z](?:\.\d{1,3}){1,5})\.?\s+(\S.*)$")
    for l in lines:
        l = l.replace("\f", "").strip()
        m = pattern.match(l)
        if not m:
            continue
        title = m.group(2).strip()
        if numbered_heading(l, NUM_HEADING):
            reason = "accepted as a heading"
        elif not title[0].isupper():
            reason = "starts with lower case or a digit"
        else:
            reason = _rejection(title, l) or "other format"
        groups.setdefault(reason, []).append(l[:90])
    return groups


def split_text(text: str, max_chars: int) -> list[str]:
    """Splits a long text by paragraphs (and by lines if a paragraph is huge)."""
    if len(text) <= max_chars:
        return [text]
    parts, buf = [], ""
    for par in re.split(r"\n\s*\n", text):
        pieces = [par] if len(par) <= max_chars else \
            [par[i:i + max_chars] for i in range(0, len(par), max_chars)]
        for t in pieces:
            if buf and len(buf) + len(t) + 2 > max_chars:
                parts.append(buf)
                buf = ""
            buf = f"{buf}\n\n{t}" if buf else t
    if buf:
        parts.append(buf)
    return parts


def prepare_sections(text: str, max_chars: int = 12000, min_chars: int = 300,
                     heading_regex: str | None = None, pdf: Path | None = None,
                     structure: str = "auto", info: dict | None = None) -> list[dict]:
    """Sections ready for analysis. Structure: the PDF 'bookmarks' if it has them, otherwise
    the numbered 'headings' of the text, otherwise fixed-'size' blocks. Sections that are
    too large are subdivided by the headings they contain before being split by size.

    `info`, if given, receives: `source` (where the structure came from), `subdivided`
    (large sections split by their inner headings) and `oversized` ((title, parts) of the
    sections split in more than 5 parts: their headings were not recognised)."""
    if structure not in STRUCTURES:
        raise ValueError(f"structure must be one of {', '.join(STRUCTURES)}, not '{structure}'")
    info = info if info is not None else {}
    raw, source = None, ""
    # Join "number" + "title" split by pdfminer before looking for the structure
    if not heading_regex and sum(1 for l in text.split("\n") if MD_HEADING.match(l)) < 2:
        text = "\n".join(_join_loose_numbers(text.split("\n")))
    if structure in ("auto", "bookmarks") and pdf is not None and Path(pdf).suffix.lower() == ".pdf":
        r = sections_from_bookmarks(Path(pdf), text)
        if r and r[2] >= max(3, r[1] * 0.5):
            raw = r[0]
            source = f"PDF bookmarks ({r[2]} of {r[1]} found in the text)"
        elif structure == "bookmarks":
            source = ("the PDF has no usable bookmarks" if not r else
                      f"only {r[2]} of {r[1]} bookmarks found") + "; headings used instead"
    if raw is None and structure != "size":
        raw = detect_sections(text, heading_regex)
        n_headings = sum(1 for s in raw if s["level"] > 0)
        source = (source + "; " if source else "") + f"text headings ({n_headings} detected)"
        if n_headings < 2:
            raw = None
    if raw is None:
        source = (source + "; " if source else "") + "fixed-size blocks"
        raw = [{"level": 1, "title": f"Block {i + 1}", "path": [f"Block {i + 1}"],
                "body": t, "page_start": 0, "page_end": 0}
               for i, t in enumerate(split_text(text.replace("\f", "\n"), max_chars))]
    info["source"] = source

    # Subdivide by their inner headings the sections that do not fit (e.g. a chapter
    # bookmark with many subsections without a bookmark of their own)
    expanded, subdivided = [], 0
    for s in raw:
        if len(s["body"]) > max_chars and structure != "size":
            subs = detect_sections(s["body"], heading_regex, s["page_start"] or 1)
            if sum(1 for x in subs if x["level"] > 0) >= 2:
                subdivided += 1
                for x in subs:
                    if x["level"] == 0:
                        x.update(level=s["level"], title=s["title"], path=list(s["path"]))
                    else:
                        x.update(level=s["level"] + x["level"], path=s["path"] + x["path"])
                    x["page_end"] = max(x["page_end"], x["page_start"])
                expanded += subs
                continue
        expanded.append(s)
    info["subdivided"] = subdivided

    # Headings without text of their own ("2 Requirements") are dropped: their title is
    # already in the path of their children. Short sections are merged with the previous
    # one, only inside the same chapter and without going over max_chars.
    merged: list[dict] = []
    for s in expanded:
        if not s["body"]:
            continue
        prev = merged[-1] if merged else None
        if (prev and prev["path"][0] == s["path"][0]
                and (len(s["body"]) < min_chars or len(prev["body"]) < min_chars)
                and len(prev["body"]) + len(s["body"]) + len(s["title"]) + 3 <= max_chars):
            prev["body"] += f"\n\n{s['title']}\n{s['body']}"
            prev["includes"].append(s["title"])
            prev["page_end"] = max(prev["page_end"], s["page_end"])
            continue
        merged.append(dict(s, includes=[]))

    # Split by size whatever is still too large
    final, oversized = [], []
    for s in merged:
        parts = split_text(s["body"], max_chars)
        if len(parts) > 5:
            oversized.append((s["title"], len(parts)))
        for k, part in enumerate(parts, 1):
            title = s["title"] if len(parts) == 1 else f"{s['title']} (part {k}/{len(parts)})"
            final.append(dict(s, title=title, body=part))
    for i, s in enumerate(final, 1):
        s["id"] = f"{i:03d}"
    info["oversized"] = oversized
    return final


def document_title(text: str, path: Path) -> str:
    """The first level-1 Markdown heading, else the first line of reasonable length,
    else the file name."""
    for line in text.split("\n")[:40]:
        m = MD_HEADING.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2).strip()
    for line in text.replace("\f", "\n").split("\n")[:15]:
        if 10 < len(line.strip()) < 120:
            return line.strip()
    return Path(path).stem
