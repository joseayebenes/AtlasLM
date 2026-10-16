"""The public declarations of the library units a project can use: the specs (.ads) of the
code roots and those of the folders of [sources.code] library (the runtime of GNAT: its
adainclude folder), read with tree-sitter-ada. For the tool library_spec and for the errors
of the compiler that name a package or a name.

    lib = Library(["~/.local/share/alire/toolchains/gnat_native_*/lib/gcc/*/*/adainclude"], roots)
    lib.spec("Ada.Command_Line")            # its subprograms with their parameters, types, constants...
    lib.close_to("Command_Line", "Arguments")   # what that package declares that looks like it
    lib.declaring("LF")                     # the units that declare LF (Ada.Characters.Latin_1)

Small models guess the names of the library: Ada.Command_Line.Arguments, a Substring that
does not exist, the attribute 'Second. The declarations are read from the specs the compiler
uses, so they are those of its version. A unit is found by its name, not by the file name
(GNAT shortens them: a-comlin.ads); a generic instance (GNAT.String_Split is new
GNAT.Array_Split) gives the declarations of its generic. The project's units go first.
The whole runtime of GNAT (896 specs) is parsed in about 0.2 s, once per process.
"""
from __future__ import annotations

import difflib
import glob
import os
import re
import threading
from pathlib import Path
from typing import Iterable

__all__ = ["Library", "with_clauses"]

MAX_DECLARATIONS = 80      # of a spec in one answer
MAX_TEXT = 160             # characters of the text of a declaration
MAX_DOC = 200              # characters of its comment
SUBPROGRAMS = ("subprogram_declaration", "expression_function_declaration", "null_procedure_declaration",
               "subprogram_renaming_declaration", "generic_subprogram_declaration", "abstract_subprogram_declaration")
TYPES = {"full_type_declaration": "type", "private_type_declaration": "type", "incomplete_type_declaration": "type",
         "private_extension_declaration": "type", "subtype_declaration": "subtype"}
OBJECTS = {"object_declaration": "object", "number_declaration": "constant", "object_renaming_declaration": "object",
           "exception_declaration": "exception", "exception_renaming_declaration": "exception"}
PACKAGES = ("package_declaration", "package_renaming_declaration", "generic_instantiation",
            "generic_package_declaration")

_cache: dict[tuple[str, int, int], dict] = {}       # file, mtime, size -> what it declares
_cache_lock = threading.Lock()


WITH = re.compile(r"^\s*(?:limited\s+)?(?:private\s+)?with\s+([\w.,\s]+?)\s*;", re.M | re.I)


def with_clauses(text: str) -> list[str]:
    """The units named in the with clauses of an Ada source."""
    return [u.strip() for m in WITH.finditer(text) for u in m.group(1).split(",") if u.strip()]


def _cut(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", text)).strip()


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def _doc(node, lines: list[str]) -> str:
    """The comment of a declaration: the lines of comment right after it (GNAT) or else
    right before it (most projects)."""
    after, i = [], node.end_point[0] + 1
    while i < len(lines) and lines[i].strip().startswith("--"):
        after.append(lines[i].strip().lstrip("-").strip())
        i += 1
    before, i = [], node.start_point[0] - 1
    while i >= 0 and lines[i].strip().startswith("--"):
        before.insert(0, lines[i].strip().lstrip("-").strip())
        i -= 1
    words = " ".join(l for l in (after or before) if l and not re.fullmatch(r"[-=*#_ ]*", l))
    return _short(words, MAX_DOC)


def _name_of(node, src: bytes) -> str:
    """The name of a unit or of a nested package: its first identifier or selected component."""
    for child in node.children:
        if child.type in ("identifier", "selected_component"):
            return _cut(child, src).strip()
        if child.type in ("package_declaration", "_package_specification"):
            return _name_of(child, src)
    return ""


def _subprogram(node, src: bytes) -> tuple[str, str, str] | None:
    """(kind, name, profile) of a subprogram node."""
    for child in node.children:
        if child.type in ("procedure_specification", "function_specification"):
            name = child.child_by_field_name("name")
            kind = "procedure" if child.type == "procedure_specification" else "function"
            return kind, _cut(name, src).split(".")[-1].strip() if name is not None else "", _flat(_cut(child, src))
        if child.type.endswith(("_specification", "_declaration")):
            found = _subprogram(child, src)
            if found:
                return found
    return None


def _declarations(package, src: bytes, lines: list[str]) -> list[dict]:
    """The declarations of the visible part of a package node: until its `private`."""
    out = []
    for child in package.children:
        if child.type == "private":
            break
        kind = child.type
        if kind in SUBPROGRAMS:
            found = _subprogram(child, src)
            if found:
                out.append({"kind": found[0], "name": found[1], "text": _short(found[2], MAX_TEXT),
                            "line": child.start_point[0] + 1, "doc": _doc(child, lines)})
        elif kind in TYPES:
            ident = next((c for c in child.children if c.type == "identifier"), None)
            if ident is not None:
                out.append({"kind": TYPES[kind], "name": _cut(ident, src), "text": _short(_flat(_cut(child, src)), MAX_TEXT),
                            "line": child.start_point[0] + 1, "doc": _doc(child, lines)})
        elif kind in OBJECTS:
            text = _flat(_cut(child, src))
            names = []
            for c in child.children:                       # the names come before the colon; then the type
                if c.type == ":":
                    break
                if c.type == "identifier":
                    names.append(_cut(c, src))
            constant = OBJECTS[kind] == "object" and re.search(r":\s*constant\b", text)
            for name in names[:1] if kind.endswith("renaming_declaration") else names:
                out.append({"kind": "constant" if constant else OBJECTS[kind], "name": name,
                            "text": _short(text, MAX_TEXT), "line": child.start_point[0] + 1, "doc": _doc(child, lines)})
        elif kind in PACKAGES:
            out.append({"kind": "package", "name": _name_of(child, src), "text": _short(_flat(_cut(child, src)), 100),
                        "line": child.start_point[0] + 1, "doc": _doc(child, lines)})
    return out


def _read(path: Path) -> dict:
    """What a spec declares: {"unit", "declarations", "generic_of"}, from the cache when the
    file has not changed."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    from atlas.code.ada import _grammar
    src = path.read_bytes()
    root = _grammar().parser.parse(src).root_node
    lines = src.decode("utf-8", errors="replace").splitlines()
    found = {"unit": "", "declarations": [], "generic_of": ""}
    for unit in (c for c in root.children if c.type == "compilation_unit"):
        for node in unit.children:
            if node.type == "generic_package_declaration":
                inner = next((c for c in node.children if c.type in ("package_declaration", "_package_specification")),
                             node)
                found.update(unit=_name_of(inner, src), declarations=_declarations(inner, src, lines))
            elif node.type == "package_declaration":
                found.update(unit=_name_of(node, src), declarations=_declarations(node, src, lines))
            elif node.type == "generic_instantiation":            # package X is new G (...): G in a call
                names = [_cut(c, src).strip() for c in node.children if c.type in ("identifier", "selected_component")]
                call = next((c for c in node.children if c.type == "function_call"), None)
                generic = next((_cut(c, src).strip() for c in (call.children if call is not None else [])
                                if c.type in ("identifier", "selected_component")), names[1] if len(names) > 1 else "")
                found.update(unit=names[0] if names else "", generic_of=generic)
            elif node.type in SUBPROGRAMS:               # a library subprogram: a unit of one declaration
                sub = _subprogram(node, src)
                if sub:
                    found.update(unit=_cut(next(c for c in node.children if c.type.endswith("_specification"))
                                           .child_by_field_name("name"), src).strip(),
                                 declarations=[{"kind": sub[0], "name": sub[1], "text": sub[2],
                                                "line": node.start_point[0] + 1, "doc": _doc(node, lines)}])
            if found["unit"]:
                break
        if found["unit"]:
            break
    with _cache_lock:
        _cache[key] = found
    return found


class Library:
    def __init__(self, folders: Iterable[str | Path] = (), roots: Iterable[str | Path] = ()):
        self.folders = [Path(p) for pattern in folders
                        for p in sorted(glob.glob(os.path.expanduser(str(pattern))), reverse=True)]
        self.roots = [Path(r) for r in roots]
        self._units: dict[str, Path] | None = None
        self._lock = threading.Lock()

    def files(self) -> list[Path]:
        """The specs: those of the code roots first (in any subfolder), then those of the folders."""
        own = [p for r in self.roots if r.is_dir() for p in sorted(r.rglob("*.ads"))]
        return own + [p for f in self.folders if f.is_dir() for p in sorted(f.glob("*.ads"))]

    def units(self) -> dict[str, Path]:
        """Each unit by its name in lower case, with its file; the first one wins."""
        with self._lock:
            if self._units is None:
                units: dict[str, Path] = {}
                for path in self.files():
                    try:
                        unit = _read(path)["unit"]
                    except OSError:
                        continue
                    if unit:
                        units.setdefault(unit.lower(), path)
                self._units = units
            return self._units

    def _unit(self, name: str, withs: Iterable[str] = ()) -> tuple[str, Path] | None:
        """(unit, file) by its full name, or by its last part (as GNAT names a package in an
        error): among the units with that last part, one of `withs` (the with clauses of the
        file), else one of the project, else of Ada, Interfaces, GNAT and System, in that order."""
        units, key = self.units(), name.strip().lower()
        if key in units:
            return key, units[key]
        same = [u for u in units if u.split(".")[-1] == key.split(".")[-1]]
        withs = {w.strip().lower() for w in withs}
        own = {p for r in self.roots for p in units.values() if r in p.parents}
        ranks = [[u for u in same if u in withs], [u for u in same if units[u] in own]]
        ranks += [[u for u in same if u.startswith(prefix)] for prefix in ("ada.", "interfaces.", "gnat.", "system.")]
        for group in ranks + [same]:
            if len(group) == 1:
                return group[0], units[group[0]]
            if group:
                return None
        return None

    def spec(self, unit: str, name: str = "", shown: callable = str) -> dict:
        """The public declarations of a unit (see the module); `name` keeps those whose name
        contains it. `shown` turns a file into how the answer shows it."""
        found = self._unit(unit)
        if found is None:
            close = difflib.get_close_matches(unit.lower(), list(self.units()), n=5, cutoff=0.6)
            return {"error": f"there is no unit {unit} in the project or in [sources.code] library"
                             + (f" (similar: {', '.join(close)})" if close else "")}
        key, path = found
        data = _read(path)
        declarations, generic = data["declarations"], data["generic_of"]
        if generic:
            inner = self._unit(generic)
            declarations = _read(inner[1])["declarations"] if inner else []
        if name:
            declarations = [d for d in declarations if name.lower() in d["name"].lower()]
        out = [{k: v for k, v in d.items() if k != "doc" or (v and len(declarations) <= 20)}
               for d in declarations[:MAX_DECLARATIONS]]
        return {"unit": data["unit"], "file": shown(path), **({"instance_of": generic} if generic else {}),
                "declarations": out, **({"truncated": True} if len(declarations) > MAX_DECLARATIONS else {})}

    def close_to(self, package: str, name: str, withs: Iterable[str] = (), n: int = 8) -> dict | None:
        """What `package` (a unit, maybe only its last part, as GNAT names it in an error)
        declares that looks like `name`; None if the unit is not known. `withs`: the with
        clauses of the file of the error."""
        found = self._unit(package, withs)
        if found is None:
            return None
        data = _read(found[1])
        declarations = data["declarations"]
        if data["generic_of"]:
            inner = self._unit(data["generic_of"])
            declarations = _read(inner[1])["declarations"] if inner else []
        by_name = {d["name"].lower(): d for d in declarations}
        close = difflib.get_close_matches(name.lower(), list(by_name), n=n, cutoff=0.5)
        close += [k for k in by_name if k.startswith(name.lower()[:4]) and k not in close][:max(0, n - len(close))]
        return {"unit": data["unit"], "similar": [by_name[k]["text"] for k in close],
                "all_names": sorted({d["name"] for d in declarations})[:60]}

    def declaring(self, name: str, n: int = 5) -> list[str]:
        """The units whose visible part declares `name` (any case), with how: "Unit: text"."""
        key, out = name.strip().lower(), []
        for path in self.files():
            data = _read(path)
            for d in data["declarations"]:
                if d["name"].lower() == key:
                    out.append(f"{data['unit']}: {d['text']}")
                    break
            if len(out) == n:
                break
        return out
