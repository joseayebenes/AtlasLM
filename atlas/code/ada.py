"""Ada analyzer with tree-sitter-ada, one file at a time (the analysis of legacy/ada_graph.py).

For each file: the `with` clauses, the subprogram declarations of the spec (their header
comment is the documentation), the subprogram bodies (lines, kind, profile, own calls with
the line and column of each one, header comment and the requirement IDs in their comments),
the package-level types and subtypes (with their source: for a record, its fields) and the
package-level variables and constants.

In Ada `Name (X)` is a call, a type conversion or an array indexing alike: here every
`Name (...)` of a body is a "call" except the local objects and parameters of that body;
the graph tells calls, conversions and uses apart once it knows every name. Names are in
lower case, because Ada does not tell case apart.

The grammar and the queries are compiled on first use, so importing this module does not
need tree-sitter. Works with py-tree-sitter 0.23 to 0.26.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from atlas.code.common import call_at, extract_requirements, requirement_pattern
from atlas.plugins import CodeAnalyzer, CodeUnit

__all__ = ["AdaAnalyzer", "PREDEFINED_TYPES", "analyze_ada"]

log = logging.getLogger(__name__)

# Each pattern is compiled on its own: if the installed grammar names a node differently,
# that pattern is dropped with a warning and the others are kept.
PATTERNS = {
    "body": [        # bodies (usually in .adb)
        "(subprogram_body (procedure_specification name: (identifier) @name)) @body",
        "(subprogram_body (function_specification name: (identifier) @name)) @body",
    ],
    "declaration": [  # declarations (usually in .ads): the documentation comes from here
        "(subprogram_declaration (procedure_specification name: (identifier) @name)) @decl",
        "(subprogram_declaration (function_specification name: (identifier) @name)) @decl",
    ],
    "call": [
        "(procedure_call_statement (identifier) @call)",
        "(function_call (identifier) @call)",
        "(procedure_call_statement (selected_component) @call)",
        "(function_call (selected_component) @call)",
    ],
    "with_clause": [
        "(with_clause (identifier) @with)",
        "(with_clause (selected_component) @with)",
    ],
    "comment": ["(comment) @c"],
    "type": [         # types and subtypes (the name is the first identifier child)
        "(full_type_declaration) @decl",
        "(subtype_declaration) @decl",
    ],
    "variable": [     # objects declared at package level (local ones are filtered out)
        "(object_declaration name: (identifier) @name) @decl",
    ],
    "local": [        # local names of a body: `Local (1)` is indexing, not a call
        "(object_declaration name: (identifier) @local)",
        "(parameter_specification) @param",
    ],
}

# Predefined types are never calls
PREDEFINED_TYPES = frozenset({
    "integer", "natural", "positive", "float", "long_float", "long_long_float",
    "short_float", "long_integer", "long_long_integer", "short_integer",
    "short_short_integer", "boolean", "character", "wide_character",
    "wide_wide_character", "string", "wide_string", "wide_wide_string", "duration",
    "unsigned_8", "unsigned_16", "unsigned_32", "unsigned_64",
    "integer_8", "integer_16", "integer_32", "integer_64",
})
KINDS = {"procedure_specification": "procedure", "function_specification": "function"}
MAX_SOURCE_CHARS = 2000  # cap for the text of a type declaration kept in the result


@lru_cache(maxsize=1)
def _grammar() -> SimpleNamespace:
    from tree_sitter import Language, Parser, Query
    import tree_sitter_ada
    try:                                   # py-tree-sitter >= 0.25
        from tree_sitter import QueryCursor
    except ImportError:                    # 0.23 / 0.24
        QueryCursor = None
    lang = Language(tree_sitter_ada.language())

    def compile_(patterns: list[str], label: str):
        valid = []
        for p in patterns:
            try:
                Query(lang, p)
                valid.append(p)
            except Exception as e:         # node or field name the grammar does not have
                log.warning("%s pattern dropped: %s (%s)", label, p, e)
        if not valid:
            log.error("no %s pattern compiles with this tree-sitter-ada", label)
            return None
        return Query(lang, "\n".join(valid))

    return SimpleNamespace(parser=Parser(lang), cursor=QueryCursor,
                           **{name: compile_(p, name) for name, p in PATTERNS.items()})


def _captures(query, node) -> dict:
    """Captures by name, each list in source order: with several patterns the binding does
    not guarantee any order (legacy/ada_graph.py changed order from one run to the next)."""
    g = _grammar()
    caps = g.cursor(query).captures(node) if g.cursor is not None else query.captures(node)
    return {k: sorted(v, key=lambda n: n.start_byte) for k, v in caps.items()}


def _matches(query, node, by: str) -> list:
    """Matches in source order of their capture `by`."""
    g = _grammar()
    found = g.cursor(query).matches(node) if g.cursor is not None else query.matches(node)
    return sorted(found, key=lambda m: m[1][by][0].start_byte)


def _text(node, src: bytes) -> str:
    """Text of a node, cut from the source by hand: node.text causes an access violation
    on Windows with some versions of the binding."""
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _norm(name: str) -> str:
    return name.strip().lower()


def _clean_comment(line: str) -> str | None:
    """Removes the leading '--'; drops decorative lines (-----, ====)."""
    s = re.sub(r"^\s*-{2,}\s?", "", line).rstrip()
    if re.fullmatch(r"[-=*#_\s]*", s):
        return None
    return s


def _leading_comment(node, lines: list[str]) -> str:
    """The block of '--' lines right above the line where the node starts. One blank line
    between the comment and the code is tolerated."""
    i = node.start_point[0] - 1
    block, blanks = [], 0
    while i >= 0:
        l = lines[i].strip()
        if l.startswith("--"):
            block.append(l)
            blanks = 0
        elif l == "" and blanks == 0 and not block:
            blanks += 1
        else:
            break
        i -= 1
    block.reverse()
    return "\n".join(c for c in (_clean_comment(x) for x in block) if c is not None).strip()


def _inner_comments(node, src: bytes) -> str:
    """Every comment inside a node (to look for requirement IDs)."""
    q = _grammar().comment
    return "\n".join(_text(c, src) for c in _captures(q, node).get("c", [])) if q else ""


def _inside_subprogram(node) -> bool:
    p = node.parent
    while p is not None:
        if p.type == "subprogram_body":
            return True
        p = p.parent
    return False


def _parameter_names(param) -> list:
    """Identifiers of a parameter_specification, without its type (subtype_mark)."""
    return [c for i, c in enumerate(param.children)
            if c.type == "identifier" and param.field_name_for_child(i) != "subtype_mark"]


def _first_identifier(node):
    return next((c for c in node.children if c.type == "identifier"), None)


def _profile(spec, src: bytes) -> str:
    """The specification of a subprogram on one line, without its comments."""
    return " ".join(re.sub(r"--[^\n]*", " ", _text(spec, src)).split())


def unit_name(path: Path) -> str:
    """Compilation unit of a file by the GNAT convention: tracks-db.ads -> tracks.db."""
    return path.stem.lower().replace("-", ".")


def analyze_ada(path: Path, src: bytes, req_pattern: re.Pattern) -> dict:
    """Everything the analysis finds in a file (see the module docstring)."""
    g = _grammar()
    lines = src.decode("utf-8", errors="replace").splitlines()
    root = g.parser.parse(src).root_node

    withs = sorted({_norm(_text(n, src)) for n in _captures(g.with_clause, root).get("with", [])}) \
        if g.with_clause else []

    declarations = []
    if g.declaration:
        for _, caps in _matches(g.declaration, root, "decl"):
            decl = caps["decl"][0]
            doc = _leading_comment(decl, lines)
            declarations.append({"name": _norm(_text(caps["name"][0], src)), "line": decl.start_point[0] + 1,
                                 "doc": doc, "reqs": extract_requirements(doc + "\n" + _text(decl, src), req_pattern)})

    types = []
    if g.type:
        for decl in _captures(g.type, root).get("decl", []):
            ident = _first_identifier(decl)
            if ident is None:
                continue
            source = _text(decl, src)
            doc = _leading_comment(decl, lines)
            types.append({"name": _norm(_text(ident, src)), "line": decl.start_point[0] + 1, "doc": doc,
                          "reqs": extract_requirements(doc, req_pattern), "source": source[:MAX_SOURCE_CHARS],
                          "record": "record" in source.lower(), "local": _inside_subprogram(decl)})

    variables = []
    if g.variable:
        for _, caps in _matches(g.variable, root, "decl"):
            decl = caps["decl"][0]
            if _inside_subprogram(decl):
                continue
            source = _text(decl, src)
            for ident in sorted(caps["name"], key=lambda n: n.start_byte):
                variables.append({"name": _norm(_text(ident, src)), "line": decl.start_point[0] + 1,
                                  "constant": " constant " in f" {source.lower()} ", "source": source[:300]})

    bodies = []
    if g.body:
        for _, caps in _matches(g.body, root, "body"):
            body = caps["body"][0]
            spec = next((c for c in body.children if c.type in KINDS), None)
            bodies.append({"name": _norm(_text(caps["name"][0], src)), "node": body,
                           "start_line": body.start_point[0] + 1, "end_line": body.end_point[0] + 1,
                           **({"kind": KINDS[spec.type], "signature": _profile(spec, src)} if spec else {})})

    # For each body: its own calls (not those of nested subprograms), header comment and
    # the requirements cited in its comments
    subprograms = []
    for b in bodies:
        node = b["node"]
        nested = [(o["node"].start_byte, o["node"].end_byte) for o in bodies
                  if o is not b and o["node"].start_byte >= node.start_byte and o["node"].end_byte <= node.end_byte]
        local_names = set()
        if g.local:
            caps = _captures(g.local, node)
            local_names = {_norm(_text(n, src)) for n in caps.get("local", [])}
            for param in caps.get("param", []):
                local_names.update(_norm(_text(n, src)) for n in _parameter_names(param))
        calls = []
        if g.call:
            for n in _captures(g.call, node).get("call", []):
                if any(s <= n.start_byte < e for s, e in nested):
                    continue
                name = _norm(_text(n, src))
                if name not in local_names:            # indexing of a local array or parameter
                    row, column = n.start_point
                    call = call_at(name, row + 1, src[n.start_byte - column:n.start_byte])
                    if call not in calls:
                        calls.append(call)
        doc = _leading_comment(node, lines)
        subprograms.append({"name": b["name"], "start_line": b["start_line"], "end_line": b["end_line"],
                            **{k: b[k] for k in ("kind", "signature") if k in b}, "calls": calls, "doc": doc,
                            "reqs": extract_requirements(doc + "\n" + _inner_comments(node, src), req_pattern)})

    return {"file": str(path), "unit": unit_name(path), "withs": withs, "declarations": declarations,
            "subprograms": subprograms, "types": types, "variables": variables}


class AdaAnalyzer(CodeAnalyzer):
    name = "ada"
    extensions = (".ads", ".adb", ".ada")
    predefined = PREDEFINED_TYPES

    def __init__(self, config: dict):
        super().__init__(config)
        self.req_pattern = requirement_pattern(config)

    def analyze_file(self, path: Path, source: bytes) -> CodeUnit:
        a = analyze_ada(Path(path), source, self.req_pattern)
        return CodeUnit(file=str(path), unit=a["unit"], language="ada", imports=a["withs"],
                        declarations=a["declarations"], subprograms=a["subprograms"],
                        types=[{k: v for k, v in t.items() if k != "local"} for t in a["types"] if not t["local"]],
                        variables=a["variables"])

    def version(self) -> str:
        return "ada-2"
