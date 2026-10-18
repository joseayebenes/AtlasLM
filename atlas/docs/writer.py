"""Deterministic Markdown output of an analysed document (section 5 of legacy/pdf_struct.py).
The model never writes files: everything comes from the sections and their analysis.

    <out>/
    ├── index.md            overview, links and table of sections
    ├── sections/NNN-*.md   one file per section: summary, requirements, source text...
    ├── requirements.md     every requirement with its verification against the text
    ├── glossary.md
    ├── entities.md
    ├── open_issues.md      TBD / TBC
    └── structure.json      all of the above (with the text of each section) for tools
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

__all__ = ["section_file", "write_document"]


def slug(t: str) -> str:
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = re.sub(r"[^\w\s-]", "", t.lower())
    return re.sub(r"[\s_-]+", "-", t).strip("-")[:50] or "section"


def cell(t: str) -> str:
    return " ".join(str(t).split()).replace("|", "\\|")


def frontmatter(fields: dict) -> str:
    # JSON is valid YAML: avoids escaping problems with quotes and colons
    return "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}"
                               for k, v in fields.items()) + "\n---\n"


def section_file(s: dict) -> str:
    return f"sections/{s['id']}-{slug(s['title'])}.md"


def pages(s: dict) -> str:
    if not s["page_start"]:
        return ""
    return str(s["page_start"]) if s["page_start"] == s["page_end"] else f"{s['page_start']}-{s['page_end']}"


def _write_section(out: Path, s: dict, source: str, reqs: list[dict]) -> None:
    a = s["analysis"]
    mine = [r for r in reqs if r["section"] == s["id"]]
    md = [frontmatter({"id": s["id"], "title": s["title"], "path": s["path"],
                       "pages": pages(s), "source": source, "includes": s["includes"],
                       "analysis_status": "error" if s.get("analysis_error") else "ok",
                       "requirements": [r["id"] for r in mine if r["id"]]}),
          f"# {s['title']}\n",
          f"> {' › '.join(s['path'])}" + (f" · pages {pages(s)}" if pages(s) else "") +
          (f"  \n> Also includes: {'; '.join(s['includes'])}" if s["includes"] else "") + "\n"]
    if s.get("analysis_error"):
        md.append(f"> ⚠ Analysis pending (it will be retried on the next run): {s['analysis_error'][:200]}\n")
    if a["summary"]:
        md.append(f"## Summary\n\n{a['summary']}\n")
    if a["key_points"]:
        md.append("## Key points\n\n" + "\n".join(f"- {p}" for p in a["key_points"]) + "\n")
    if mine:
        md.append("## Requirements\n\n| ID | Text | Verification |\n|---|---|---|\n" +
                  "\n".join(f"| {cell(r['id']) or '—'} | {cell(r['text'])} | {r['status']} |" for r in mine) + "\n")
    if a["definitions"]:
        md.append("## Definitions\n\n" + "\n".join(
            f"- **{d['term']}**: {d['definition']}" for d in a["definitions"]) + "\n")
    if a["entities"]:
        md.append("## Entities\n\n" + "\n".join(
            f"- **{e['name']}** ({e['kind']})" + (f": {e['description']}" if e["description"] else "")
            for e in a["entities"]) + "\n")
    if a["references"]:
        md.append("## References\n\n" + "\n".join(f"- {r}" for r in a["references"]) + "\n")
    if a["open_issues"]:
        md.append("## Open issues\n\n" + "\n".join(f"- {p}" for p in a["open_issues"]) + "\n")
    md.append("## Source text\n\n~~~~text\n" + s["body"] + "\n~~~~\n")
    (out / section_file(s)).write_text("\n".join(md), encoding="utf-8", newline="\n")


def write_document(out: Path, doc_title: str, source: str, model: str, sections: list[dict],
                   overview: dict, cons: dict, extra: dict | None = None) -> dict:
    """Writes every file of the document into `out` and returns the content of
    structure.json. `extra` is added to structure.json as it is."""
    out = Path(out)
    (out / "sections").mkdir(parents=True, exist_ok=True)
    reqs = cons["requirements"]
    for s in sections:
        _write_section(out, s, source, reqs)

    status = Counter(r["status"] for r in reqs)
    by_id = {s["id"]: s for s in sections}
    link = lambda sid: f"[{sid}]({section_file(by_id[sid])})"
    more = lambda s: f" + {len(s['includes'])} subsections" if s["includes"] else ""

    # index.md
    md = [frontmatter({"title": doc_title, "source": source, "model": model,
                       "generated": datetime.now().isoformat(timespec="seconds"),
                       "sections": len(sections), "requirements": len(reqs)}),
          f"# {doc_title}\n"]
    if overview["summary"]:
        md.append(f"## Summary\n\n{overview['summary']}\n")
    if overview["purpose"] or overview["scope"]:
        md.append(f"**Purpose:** {overview['purpose']}\n\n**Scope:** {overview['scope']}\n")
    if overview["main_topics"]:
        md.append("## Main topics\n\n" + "\n".join(f"- {t}" for t in overview["main_topics"]) + "\n")
    md.append("## Documents\n\n- [Requirements](requirements.md) — "
              f"{len(reqs)} ({status['literal']} literal, {status['partial']} partial, "
              f"{status['not_found']} not found in the text)\n"
              f"- [Glossary](glossary.md) — {len(cons['glossary'])} terms\n"
              f"- [Entities](entities.md) — {len(cons['entities'])}\n"
              f"- [Open issues](open_issues.md) — {len(cons['open_issues'])}\n")
    md.append("## Sections\n\n| # | Section | Pages | Req. | Summary |\n|---|---|---|---|---|\n" +
              "\n".join(
                  f"| {s['id']} | [{cell(s['title'])}]({section_file(s)}){more(s)} | {pages(s)} | "
                  f"{sum(1 for r in reqs if r['section'] == s['id'])} | "
                  f"{'⚠ analysis pending' if s.get('analysis_error') else cell(s['analysis']['summary'].split('. ')[0])[:160]} |"
                  for s in sections) + "\n")
    (out / "index.md").write_text("\n".join(md), encoding="utf-8", newline="\n")

    # requirements.md
    md = [frontmatter({"title": f"Requirements — {doc_title}", "count": len(reqs), "verification": dict(status)}),
          "# Requirements\n",
          "The Verification column says whether the extracted text appears verbatim in the "
          "document. Check the `not_found` ones by hand.\n",
          "| ID | Text | Section | Verification |\n|---|---|---|---|"]
    md += [f"| {cell(r['id']) or '—'} | {cell(r['text'])} | {link(r['section'])} | {r['status']} |" for r in reqs]
    (out / "requirements.md").write_text("\n".join(md) + "\n", encoding="utf-8", newline="\n")

    # glossary.md
    md = [frontmatter({"title": f"Glossary — {doc_title}", "count": len(cons["glossary"])}),
          "# Glossary\n", "| Term | Definition | Sections |\n|---|---|---|"]
    md += [f"| **{cell(g['term'])}** | {cell(' / '.join(g['definitions'][:3]))} | "
           f"{', '.join(link(x) for x in g['sections'])} |" for g in cons["glossary"]]
    (out / "glossary.md").write_text("\n".join(md) + "\n", encoding="utf-8", newline="\n")

    # entities.md (grouped by kind)
    md = [frontmatter({"title": f"Entities — {doc_title}", "count": len(cons["entities"])}), "# Entities\n"]
    kind = None
    for e in cons["entities"]:
        if e["kind"] != kind:
            kind = e["kind"]
            md.append(f"\n## {kind}\n")
        md.append(f"- **{e['name']}**" + (f": {e['description']}" if e["description"] else "") +
                  f" — {', '.join(link(x) for x in e['sections'])}")
    (out / "entities.md").write_text("\n".join(md) + "\n", encoding="utf-8", newline="\n")

    # open_issues.md
    md = [frontmatter({"title": f"Open issues — {doc_title}", "count": len(cons["open_issues"])}),
          "# Open issues (TBD / TBC)\n"]
    md += [f"- {p['text']} — {link(p['section'])}" for p in cons["open_issues"]] or ["None."]
    (out / "open_issues.md").write_text("\n".join(md) + "\n", encoding="utf-8", newline="\n")

    # structure.json: everything together for tools (index, graph, MCP)
    data = {"title": doc_title, "source": source, "model": model, "overview": overview,
            "sections": [{k: s[k] for k in ("id", "title", "path", "includes", "page_start",
                                             "page_end", "analysis", "body")}
                         | {"file": section_file(s), "analysis_error": s.get("analysis_error", "")}
                         for s in sections],
            **cons, **(extra or {})}
    (out / "structure.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8",
                                        newline="\n")
    return data
