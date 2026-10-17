"""build_context: everything relevant about a requirement, a code element or a unit, in one
Markdown package that fits a budget of tokens, without a model (the context_builder of the
plan). For an agent that is going to change that code (OpenCode, VS Code): one call instead
of ten searches.

The package is made of blocks in order of importance; a block that does not fit is cut at a
line and the rest is left out (`omitted`). Tokens are estimated as characters / 4.

    requirement   the requirement (DOORS text, links), the sections that state it, the code
                  that implements it (signatures), its tests, the callers of that code, and
                  the source of that code
    code element  the element (signature, comment), its requirements, its callers and
                  callees, its tests, the sections about it, and its source
    unit          what it defines (signatures, comments, requirements), its requirements,
                  its tests, the units that use it, and the source of its spec

A requirement also gets, as the last block (the first one cut), requirements like it that
already have code (similar_requirements): an example of how this project implements and
tags one, so that the agent follows it.

similar_requirements finds them without a model, in this order: those linked to it in DOORS
(links either way, its parent) and those stated in the same sections; then, with an index,
those whose text the search finds by IDs and words (no vectors). Only those with code count.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from atlas.agents.link import linked_requirements
from atlas.graph.knowledge import REQUIREMENT_KINDS, KnowledgeGraph

__all__ = ["build_context", "similar_requirements"]

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
MAX_ITEMS = 25          # elements of a list inside a block
MAX_SIMILAR = 10        # similar requirements at most
BODY_LINES = 20         # lines of the body of the first similar requirement
TAG_ABOVE = 5           # lines above an element where its Req: tag may be


def _signature(g: KnowledgeGraph, n: str) -> str:
    """The first lines of a subprogram up to `is` or `;`, or the declaration of a type or
    variable."""
    d = g.G.nodes[n]
    if d.get("source"):
        return d["source"].strip().splitlines()[0][:200]
    if d.get("kind") in ("subprogram", "test") and d.get("start_line"):
        path = Path(d["file"]) if Path(d["file"]).is_absolute() else g.root / d["file"]
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        out = []
        for line in lines[d["start_line"] - 1:d["start_line"] + 4]:
            out.append(line.strip())
            if line.rstrip().endswith((" is", ";")) or " is" in line:
                break
        return " ".join(out)[:300]
    return ""


def _element(g: KnowledgeGraph, n: str, with_requirements: bool = True) -> str:
    d = g.G.nodes[n]
    where = f"{d.get('file', '')}:{d.get('start_line') or d.get('line') or ''}".rstrip(":")
    lines = [f"- `{n}` ({d.get('kind')}, {where})"]
    signature = _signature(g, n)
    if signature:
        lines.append(f"  `{signature}`")
    if d.get("doc"):
        lines.append("  " + d["doc"].split("\n")[0][:200])
    if with_requirements and d.get("requirements"):
        lines.append("  Requirements: " + ", ".join(d["requirements"]))
    return "\n".join(lines)


def _elements(g: KnowledgeGraph, nodes, with_requirements: bool = True) -> str:
    nodes = list(nodes)
    text = "\n".join(_element(g, n, with_requirements) for n in nodes[:MAX_ITEMS])
    return text + (f"\n- … and {len(nodes) - MAX_ITEMS} more" if len(nodes) > MAX_ITEMS else "")


def _requirements(g: KnowledgeGraph, ids) -> str:
    out = []
    for r in sorted(set(ids))[:MAX_ITEMS]:
        d = g.G.nodes[r] if r in g.G else {}
        out.append(f"- **{r}**" + (f" ({d['module']})" if d.get("module") else " (not in DOORS)")
                   + (f": {d['text']}" if d.get("text") else ""))
    return "\n".join(out)


def _sections(items: list[dict]) -> str:
    out = []
    for x in items[:MAX_ITEMS]:
        line = f"- {x['document']} § {x['title']}"
        if x.get("text"):
            line += f": {x['text']}"
        if x.get("values_differ"):
            v = x["values_differ"]
            line += f" **(values differ from DOORS: {', '.join(v['doors'])} vs {', '.join(v['document'])})**"
        out.append(line)
    return "\n".join(out)


def _callers(g: KnowledgeGraph, nodes) -> list[str]:
    """Who calls any of these elements, without themselves."""
    found = {i["id"] for n in nodes for i in g.callers(n, limit=MAX_ITEMS * 4).get("items", [])}
    return sorted(found - set(nodes))


def _source(g: KnowledgeGraph, nodes) -> str:
    out = []
    for n in nodes:
        src = g.read_source(n)
        if "source" in src:
            out.append(f"`{n}` ({src.get('file', '')}):\n```\n{src['source']}\n```")
    return "\n\n".join(out)


def _requirement_blocks(g: KnowledgeGraph, r: str) -> list[tuple[str, str]]:
    t = g.trace(r)
    head = [f"**{r}**" + (f" ({t['module']})" if t.get("module") else " (not in DOORS)")
            + (f": {t['text']}" if t.get("text") else "")]
    attrs = {k: v for k, v in (t.get("attributes") or {}).items() if v not in ("", None)}
    if attrs:
        head.append("Attributes: " + ", ".join(f"{k} = {v}" for k, v in attrs.items()))
    if t.get("parent"):
        head.append(f"Parent: {t['parent']}")
    for link in t.get("links_out", []):
        target = g.G.nodes[link["id"]].get("text", "")
        head.append(f"{link['type']} {link['id']}" + (f": {target}" if target else ""))
    for link in t.get("links_in", []):
        head.append(f"{link['id']} {link['type']} it")
    code = [i["id"] for i in t["implemented_by"]["items"]]
    return [("Requirement", "\n".join(head)),
            ("Documents that state it", _sections(t["documents"])),
            ("Code that implements it", _elements(g, code)),
            ("Tests", "\n".join(f"- `{x['id']}` ({'cites it' if x['how'] == 'tag' else 'calls its code'})"
                                for x in t["verified_by"])),
            ("Callers of that code", _elements(g, _callers(g, code))),
            ("Source", _source(g, [n for n in code if g.G.nodes[n].get("kind") in ("subprogram", "type")]))]


def _code_blocks(g: KnowledgeGraph, n: str) -> list[tuple[str, str]]:
    t = g.trace_code(n)
    calls = t.get("callees", {})
    return [("Element", _element(g, n)),
            ("Requirements", _requirements(g, [r["id"] for r in t["requirements"]])),
            ("Called by", _elements(g, t.get("callers", []), False)),
            ("Calls", _elements(g, calls.get("calls", []), False)
             + ("\nConverts to: " + ", ".join(calls["converts"]) if calls.get("converts") else "")
             + ("\nUses: " + ", ".join(calls["uses"]) if calls.get("uses") else "")),
            ("Exercises", "\n".join(f"- `{x}`" for x in t["exercises"])) if "exercises" in t else
            ("Tests", "\n".join(f"- `{x}`" for x in t.get("tests", []))),
            ("Documents", _sections(t["documents"])),
            ("Source", _source(g, [n]))]


def _unit_blocks(g: KnowledgeGraph, n: str) -> list[tuple[str, str]]:
    info = g.unit_info(n)
    defined = [m for kind in ("subprogram", "test") for m in info["defines"].get(kind, [])]
    data = [m for kind in ("type", "variable", "constant") for m in info["defines"].get(kind, [])]
    t = g.trace_code(n)
    unit_users = sorted({g.G.nodes[c].get("unit") or c for c in _callers(g, defined)})
    spec = next((f for f in info["files"] if f.endswith((".ads", ".h", ".hpp"))), "")
    return [("Unit", f"`{n}`, files: {', '.join(info['files'])}"
                     + (f"\nWith: {', '.join(info['withs'])}" if info["withs"] else "")),
            ("Subprograms", _elements(g, defined)),
            ("Types and variables", _elements(g, data)),
            ("Requirements", _requirements(g, [r["id"] for r in t["requirements"]])),
            ("Tests", "\n".join(f"- `{x}`" for x in t.get("tests", []))),
            ("Used by", ", ".join(f"`{u}`" for u in unit_users)),
            ("Spec", f"{spec}:\n```\n{g.read_file(spec, 1, 200)['source']}\n```"
             if spec and "source" in g.read_file(spec, 1, 1) else "")]


def _read(g: KnowledgeGraph, file: str) -> list[str]:
    path = Path(file) if Path(file).is_absolute() else g.root / file
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _tag(g: KnowledgeGraph, n: str, r: str) -> dict | None:
    """Where the code of `n` cites requirement `r`: {"file", "line", "text"}, the nearest
    line above or inside its source (that of read_source, with the lines of its comment),
    or else above its declaration in the spec. None if neither cites it."""
    d = g.G.nodes[n]
    cites = re.compile(rf"(?<![\w-]){re.escape(r)}(?![\w-])", re.IGNORECASE)
    declared = re.compile(rf"(?i)\b(?:procedure|function|type|subtype|def|class)\s+{re.escape(d.get('name', ''))}\b")
    start = d.get("start_line") or d.get("line") or 0
    places = [(d.get("file"), start, d.get("end_line") or start)] if start else []
    if d.get("spec_file") and d["spec_file"] != d.get("file"):
        places.append((d["spec_file"], 0, 0))
    for file, lo, hi in places:
        lines = _read(g, file or "")
        if not lo:                                   # a spec: by the declaration of its name
            lo = hi = next((i + 1 for i, line in enumerate(lines) if declared.search(line)), 0)
        hits = [i for i in range(max(1, lo - TAG_ABOVE), min(hi, len(lines)) + 1) if cites.search(lines[i - 1])]
        if hits:
            return {"file": file, "line": hits[-1], "text": lines[hits[-1] - 1].strip()}
    return None


def _example(g: KnowledgeGraph, r: str, why: list[str], body: bool) -> dict:
    """A similar requirement with its code: text, why, the element that implements it (a
    subprogram first) and its signature, its Req: tag, and with `body` its first lines."""
    code = g._code_of(r)
    n = next((c for c in code if g._kind(c) in ("subprogram", "test")), code[0])
    text = g.G.nodes[r].get("text") or next((x["text"] for x in g._documents(r) if x.get("text")), "")
    out = {"id": r, "text": text, "why": why, "code": n, "signature": _signature(g, n), "tag": _tag(g, n, r)}
    if len(code) > 1:
        out["also"] = [c for c in code if c != n][:5]
    if body:
        source = g.read_source(n)
        if "source" in source:
            out["body"] = "\n".join(source["source"].splitlines()[:BODY_LINES])
    return out


def similar_requirements(g: KnowledgeGraph, req_id: str, k: int = 3, index=None) -> dict:
    """Up to `k` requirements like `req_id` that already have code, without a model: those
    linked to it in DOORS (either way, and its parent) and those stated in the same sections
    (the sources of atlas.agents.link.candidates); then, with `index` (an atlas.index.Index),
    those whose text the search finds, by IDs and words. {"id", "count", "similar": [{"id",
    "text", "why", "code", "signature", "tag", "body" (the first one)}]} or {"error"}."""
    r, err = g._requirement(req_id)
    if err:
        return err
    k = max(1, min(k, MAX_SIMILAR))
    t, found = g.trace(r), {}

    def add(x: str | None, why: str) -> None:
        if x and x != r and x in g.G and g._kind(x) in REQUIREMENT_KINDS and g._code_of(x):
            reasons = found.setdefault(x, [])
            if why not in reasons:
                reasons.append(why)

    for x in linked_requirements(t, g):
        add(x["id"], f"linked in DOORS ({x['type']})")
    add(t.get("parent"), "its parent in DOORS")
    for section in t["documents"]:
        for x in g._linked(section["id"], {"declares"}, incoming=False):
            add(x, f"stated in the same section, {section['document']} › {section['title']}")
    text = t.get("text") or next((x["text"] for x in t["documents"] if x.get("text")), "")
    if index is not None and text and len(found) < k:
        try:
            hits = index.search(text, k=k * 4, kinds=["requirement"], dense=False, per_document=0)
        except Exception as e:                       # an index that is not there: DOORS is enough
            log.warning("similar requirements of %s: no search (%s)", r, e)
            hits = []
        for h in hits:
            add(g._lower.get(str(h.get("ref") or "").lower()), "its text is alike (search)")
    similar = [_example(g, x, why, body=i == 0) for i, (x, why) in enumerate(list(found.items())[:k])]
    return {"id": r, "count": len(similar), "similar": similar}


def _similar_block(g: KnowledgeGraph, r: str, index=None) -> str:
    out = []
    for x in similar_requirements(g, r, index=index)["similar"]:
        d = g.G.nodes[x["code"]]
        where = f"{d.get('file', '')}:{d.get('start_line') or d.get('line') or ''}".rstrip(":")
        lines = [f"- **{x['id']}** ({'; '.join(x['why'])})" + (f": {x['text']}" if x["text"] else ""),
                 f"  `{x['code']}` ({where})" + (f": `{x['signature']}`" if x["signature"] else "")]
        if x["tag"]:
            lines.append(f"  Tag: {x['tag']['file']}:{x['tag']['line']} `{x['tag']['text']}`")
        if x.get("body"):
            lines.append(f"\n```\n{x['body']}\n```")
        out.append("\n".join(lines))
    return "\n".join(out)


def build_context(g: KnowledgeGraph, target: str, budget_tokens: int = 4000, index=None) -> dict:
    """The package of `target` (a requirement ID, a code identifier or simple name, a unit,
    or "file:line"). `index` (an atlas.index.Index) adds the similar requirements its search
    finds. {"target", "kind", "markdown", "tokens", "truncated", "omitted"} or {"error"}."""
    n, err = g.resolve(target)
    if err:
        return err
    kind = g.G.nodes[n].get("kind")
    if kind in REQUIREMENT_KINDS:
        blocks = _requirement_blocks(g, n) + [("Similar requirements already implemented",
                                               _similar_block(g, n, index))]     # the first one cut
    elif kind == "unit":
        blocks = _unit_blocks(g, n)
    elif kind in ("subprogram", "test", "type", "variable", "constant"):
        blocks = _code_blocks(g, n)
    else:
        return {"error": f"{n} is a {kind}: build_context takes a requirement, a code element or a unit"}
    budget = max(200, budget_tokens) * CHARS_PER_TOKEN
    text, omitted, truncated = f"# Context of {n} ({kind})\n", [], False
    for title, body in blocks:
        if not body.strip():
            continue
        block = f"\n## {title}\n\n{body.strip()}\n"
        if truncated:
            omitted.append(title)
            continue
        if len(text) + len(block) > budget:
            room = budget - len(text) - 40
            lines, kept = block.splitlines(keepends=True), []
            for line in lines:
                if sum(map(len, kept)) + len(line) > room:
                    break
                kept.append(line)
            if len(kept) > 3:                       # more than the title: keep what fits
                text += "".join(kept) + "\n[…cut to fit the budget]\n"
            else:
                omitted.append(title)
            truncated = True
            continue
        text += block
    return {"target": n, "kind": kind, "markdown": text, "tokens": len(text) // CHARS_PER_TOKEN,
            "truncated": truncated, "omitted": omitted}
