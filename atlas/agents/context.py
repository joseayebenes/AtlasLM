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
"""
from __future__ import annotations

from pathlib import Path

from atlas.graph.knowledge import REQUIREMENT_KINDS, KnowledgeGraph

__all__ = ["build_context"]

CHARS_PER_TOKEN = 4
MAX_ITEMS = 25          # elements of a list inside a block


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
            line += (f" **(values differ from DOORS: {', '.join(v['doors'])} vs {', '.join(v['document'])})**"
                     if "doors" in v else
                     f" **(values differ from {v['against']}: {', '.join(v['official'])} vs "
                     f"{', '.join(v['document'])})**")
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


def build_context(g: KnowledgeGraph, target: str, budget_tokens: int = 4000) -> dict:
    """The package of `target` (a requirement ID, a code identifier or simple name, or a
    unit). {"target", "kind", "markdown", "tokens", "truncated", "omitted"} or {"error"}."""
    n, err = g.resolve(target)
    if err:
        return err
    kind = g.G.nodes[n].get("kind")
    if kind in REQUIREMENT_KINDS:
        blocks = _requirement_blocks(g, n)
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
