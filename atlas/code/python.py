"""Python analyzer with the standard library (ast and tokenize), one file at a time.

For each file: the imports, the functions and methods (lines, own calls, docstring and
header comments, and the requirement IDs in their comments and docstring), the classes (as
types) and the module-level variables and constants (a name in upper case is a constant).

A module is named by its dotted path from the code root that holds it:
tests/python/test_session.py under the root "tests" is "python.test_session", so that it
does not meet an Ada unit of the same name. A method is "Class.method" and a nested
function "outer.inner". Names keep their case, because Python tells case apart; calls to
builtins (print, len...) are left out, like the predefined types of Ada.

A requirement tag counts for a function when it is in the comments right above it (or above
its decorators), in its docstring or in a comment of its body; one at module level cites
nothing. A file that does not parse gives an empty unit and a warning: it may be half
written by an agent.
"""
from __future__ import annotations

import ast
import builtins
import io
import logging
import re
import tokenize
from pathlib import Path

from atlas.code.common import extract_requirements, requirement_ids, requirement_pattern
from atlas.plugins import CodeAnalyzer, CodeUnit

__all__ = ["PythonAnalyzer", "analyze_python", "module_name"]

log = logging.getLogger(__name__)

BUILTINS = set(dir(builtins))
MAX_SOURCE_CHARS = 2000  # cap for the text of a class kept in the result
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def module_name(path: Path, roots: list[str] | tuple[str, ...] = ()) -> str:
    """The dotted name of a file from the root that holds it (the deepest one), or its
    stem if no root does."""
    p = Path(path).resolve()
    best: Path | None = None
    for r in roots:
        root = Path(r).expanduser().resolve()
        if p.is_relative_to(root) and (best is None or len(root.parts) > len(best.parts)):
            best = root
    parts = list(p.relative_to(best).with_suffix("").parts) if best else [p.stem]
    if len(parts) > 1 and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _comments(text: str) -> dict[int, str]:
    """Line -> text of the comment on that line (without '#')."""
    out = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string.lstrip("#").strip()
    except (tokenize.TokenError, SyntaxError, IndentationError):
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                out[n] = line.strip().lstrip("#").strip()
    return out


def _leading_comment(first_line: int, lines: list[str], comments: dict[int, str]) -> str:
    """The block of comment lines right above `first_line` (the def or its first decorator)."""
    block, n = [], first_line - 1
    while n >= 1 and n in comments and lines[n - 1].strip().startswith("#"):
        block.append(comments[n])
        n -= 1
    return "\n".join(reversed(block))


def _dotted(node: ast.AST) -> str:
    """'a.b.c' for a name or a chain of attributes of a name; '' otherwise."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _own_nodes(node: ast.AST):
    """The nodes of a function or class body, without those of the functions and classes
    nested in it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (*FUNCTIONS, ast.ClassDef)):
            continue
        yield child
        yield from _own_nodes(child)


def _first_line(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *(d.lineno for d in decorators)])


def analyze_python(path: Path, src: bytes, req_pattern: re.Pattern, roots=(),
                   req_ids: re.Pattern | None = None) -> dict:
    """Everything the analysis finds in a file (see the module docstring)."""
    text = src.decode("utf-8", errors="replace")
    unit = module_name(path, roots)
    empty = {"file": str(path), "unit": unit, "imports": [], "subprograms": [], "types": [], "variables": []}
    try:
        tree = ast.parse(text, filename=str(path))
    except (SyntaxError, ValueError) as e:
        log.warning("%s does not parse as Python, analysed as empty: %s", path, e)
        return empty
    lines = text.splitlines()
    comments = _comments(text)

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.module or node.level):
            imports.add("." * node.level + (node.module or ""))

    subprograms, types = [], []

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNCTIONS):
                name = ".".join([*scope, child.name])
                start, end = _first_line(child), child.end_lineno or child.lineno
                nested = [(_first_line(n), n.end_lineno or n.lineno) for n in ast.walk(child)
                          if n is not child and isinstance(n, (*FUNCTIONS, ast.ClassDef))]
                inner = "\n".join(c for n, c in comments.items() if start <= n <= end
                                  and not any(s <= n <= e for s, e in nested))
                doc = "\n".join(x for x in (_leading_comment(start, lines, comments),
                                            ast.get_docstring(child) or "") if x)
                calls = set()
                for n in _own_nodes(child):
                    if isinstance(n, ast.Call):
                        called = _dotted(n.func)
                        if called and called not in BUILTINS:
                            calls.add(called)
                subprograms.append({"name": name, "kind": "function", "start_line": start, "end_line": end,
                                    "calls": sorted(calls),
                                    "doc": doc, "reqs": extract_requirements(doc + "\n" + inner, req_pattern, req_ids)})
                visit(child, [*scope, child.name])
            elif isinstance(child, ast.ClassDef):
                start = _first_line(child)
                doc = "\n".join(x for x in (_leading_comment(start, lines, comments),
                                            ast.get_docstring(child) or "") if x)
                source = ast.get_source_segment(text, child) or ""
                types.append({"name": ".".join([*scope, child.name]), "line": start, "doc": doc,
                              "reqs": extract_requirements(doc, req_pattern, req_ids), "source": source[:MAX_SOURCE_CHARS],
                              "record": False})
                visit(child, [*scope, child.name])

    visit(tree, [])

    variables = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else \
            [node.target] if isinstance(node, ast.AnnAssign) else []
        source = (ast.get_source_segment(text, node) or "")[:300]
        for t in targets:
            for name in ([t] if isinstance(t, ast.Name) else [e for e in getattr(t, "elts", []) if isinstance(e, ast.Name)]):
                variables.append({"name": name.id, "line": node.lineno, "constant": name.id.isupper(),
                                  "source": source})

    return {**empty, "imports": sorted(imports), "subprograms": subprograms, "types": types, "variables": variables}


class PythonAnalyzer(CodeAnalyzer):
    name = "python"
    extensions = (".py",)

    def __init__(self, config: dict):
        super().__init__(config)
        self.req_pattern = requirement_pattern(config)
        self.req_ids = requirement_ids(config)
        self.roots = list(config.get("roots", []))

    def analyze_file(self, path: Path, source: bytes) -> CodeUnit:
        a = analyze_python(Path(path), source, self.req_pattern, self.roots, self.req_ids)
        return CodeUnit(file=str(path), unit=a["unit"], language="python", imports=a["imports"],
                        subprograms=a["subprograms"], types=a["types"], variables=a["variables"])

    def version(self) -> str:
        return "python-2"     # kind
