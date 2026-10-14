"""The subprograms of a source file, with their lines, for the tools that change a whole
subprogram (replace_subprogram, add_subprogram): Ada with tree-sitter-ada, Python with ast.

    spans = subprograms(".adb", text)        # [Span(name, kind, start, end, parents)], None if not supported
    found = new_subprograms(".adb", text)    # the subprograms of a new text, parsed inside a package
    where = insertion_line(".adb", text)     # the line before which a new subprogram goes

A span runs from the first line of the subprogram (with `overriding`, or its decorators in
Python) to its last one (`end Name;`). Its kind is "body" (a body, an expression function, a
null procedure, a stub) or "declaration" (a spec without body): in Ada a name can have both in
the same file. `parents` are the names of the subprograms and packages around it, outermost
first. Names of Ada are compared without case.
"""
from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass, field

__all__ = ["Span", "comment_mark", "insertion_line", "language", "new_subprograms", "subprograms"]

ADA = (".ads", ".adb", ".ada")
PYTHON = (".py",)
ADA_KINDS = {"subprogram_body": "body", "expression_function_declaration": "body",
             "null_procedure_declaration": "body", "subprogram_body_stub": "body",
             "subprogram_declaration": "declaration", "generic_subprogram_declaration": "declaration",
             "subprogram_renaming_declaration": "declaration"}
WRAPPER = "Atlas_Wrapper__"


@dataclass
class Span:
    name: str
    kind: str                       # "body" or "declaration"
    start: int                      # first line, 1-based
    end: int                        # last line
    parents: list[str] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return ".".join(self.parents + [self.name])


def language(suffix: str) -> str | None:
    suffix = suffix.lower()
    return "ada" if suffix in ADA else "python" if suffix in PYTHON else None


def comment_mark(suffix: str) -> str:
    return "--" if language(suffix) == "ada" else "#"


# --- Ada --------------------------------------------------------------------------------------

def _ada_tree(src: bytes):
    from atlas.code.ada import _grammar
    return _grammar().parser.parse(src)


def _cut(node, src: bytes) -> str:
    """The text of a node, cut from the source by hand (as atlas.code.ada does: node.text
    breaks on Windows with some versions of the binding)."""
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _ada_name(node, src: bytes, depth: int = 0) -> str | None:
    """The name of a subprogram node: the `name` of its procedure or function specification
    (inside a generic declaration, one level down)."""
    for child in node.children:
        if child.type in ("procedure_specification", "function_specification"):
            name = child.child_by_field_name("name")
            if name is not None:
                return _cut(name, src).split(".")[-1].strip()
        elif depth == 0 and child.type.endswith(("_specification", "_declaration")):
            found = _ada_name(child, src, 1)
            if found:
                return found
    return None


def _ada_spans(text: str) -> list[Span]:
    src = text.encode("utf-8")
    root = _ada_tree(src).root_node
    out: list[Span] = []

    def walk(node, parents: list[str]) -> None:
        for child in node.children:
            kind = ADA_KINDS.get(child.type)
            if kind:
                name = _ada_name(child, src)
                if name:
                    out.append(Span(name, kind, child.start_point[0] + 1, child.end_point[0] + 1, list(parents)))
                    walk(child, parents + [name])
                    continue
            if child.type in ("package_body", "package_declaration"):
                ident = next((c for c in child.children if c.type in ("identifier", "selected_component")), None)
                walk(child, parents + ([_cut(ident, src).strip()] if ident is not None else []))
                continue
            walk(child, parents)

    walk(root, [])
    return out


def _ada_insertion(text: str) -> tuple[int, int] | None:
    """(line, indentation) where a new subprogram goes in an Ada package: before `private` in a
    spec, before the `begin` of a body that has one, else before its `end`. None if the file
    is not a package."""
    root = _ada_tree(text.encode("utf-8")).root_node
    package = next((c for unit in root.children if unit.type == "compilation_unit" for c in unit.children
                    if c.type in ("package_body", "package_declaration")), None)    # the with clauses are units too
    if package is None:
        return None
    stops = [c for c in package.children if c.type in ("private", "begin")] or \
            [c for c in package.children if c.type == "end"]
    if not stops:
        return None
    lines = text.split("\n")
    items = [c for c in package.children if c.is_named and c.type not in ("identifier", "selected_component",
                                                                          "comment")]
    declared = [c for item in items for c in ([item] if ADA_KINDS.get(item.type) else item.children)
                if ADA_KINDS.get(c.type)]
    if declared:
        line = lines[declared[0].start_point[0]]
        indent = len(line) - len(line.lstrip())
    else:
        first = lines[package.start_point[0]]
        indent = len(first) - len(first.lstrip()) + 3
    return stops[0].start_point[0] + 1, indent


# --- Python ------------------------------------------------------------------------------------

def _python_spans(text: str) -> list[Span]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[Span] = []

    def walk(node, parents: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                out.append(Span(child.name, "body", start, child.end_lineno, list(parents)))
                walk(child, parents + [child.name])
            elif isinstance(child, ast.ClassDef):
                walk(child, parents + [child.name])

    walk(tree, [])
    return out


# --- both --------------------------------------------------------------------------------------

def subprograms(suffix: str, text: str) -> list[Span] | None:
    """The subprograms of a file by its extension; None for a language it does not know."""
    lang = language(suffix)
    if lang == "ada":
        return _ada_spans(text)
    if lang == "python":
        return _python_spans(text)
    return None


def new_subprograms(suffix: str, text: str) -> list[Span]:
    """The subprograms of a new text at its top level, with their lines in the text: in Ada
    parsed inside a package (a spec for .ads, a body otherwise), in Python dedented."""
    lang = language(suffix)
    if lang == "ada":
        head = f"package {WRAPPER} is" if suffix.lower() == ".ads" else f"package body {WRAPPER} is"
        spans = _ada_spans(f"{head}\n{text}\nend {WRAPPER};\n")
        return [Span(s.name, s.kind, s.start - 1, s.end - 1, []) for s in spans if s.parents == [WRAPPER]]
    if lang == "python":
        return [s for s in _python_spans(textwrap.dedent(text)) if not s.parents]
    return []


def insertion_line(suffix: str, text: str) -> tuple[int, int] | None:
    """(line, indentation) before which new subprograms go when no `after` is given: in an
    Ada package, at the end of its declarations; in Python, after the last line. None if
    there is no such place."""
    lang = language(suffix)
    if lang == "ada":
        return _ada_insertion(text)
    if lang == "python":
        return len(text.rstrip("\n").split("\n")) + 1, 0
    return None
