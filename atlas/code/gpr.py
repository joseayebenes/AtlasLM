"""The GPR project of an Ada code base as the source of its folders and of its build commands.

    read_project(path, vars)   {name, path, source_dirs, object_dirs, withs, main, warnings}
    code_config(cfg)           [sources.code] with what its `project` gives: roots, exclude,
                               check_command, test_command, language_server_project

A real Ada project already says in its .gpr files where its sources are (Source_Dirs), where
the compiler leaves what it produces (Object_Dir) and which other projects it uses (with); the
scenario variables (external) choose between builds. With `[sources.code] project = "tms.gpr"`
atlas reads that instead of asking for it again:
- roots, when not given, are the source folders of the project and of the projects it
  imports (recursively), the ones inside each other once;
- the object, executable and library folders go into exclude (not the ones that hold
  sources: the default Object_Dir is the folder of the project);
- check_command and test_command, when not given, are gprbuild over the project, with one
  -X per [sources.code.project_vars]; a spec alone (.ads) is checked with -gnatc, since GNAT
  cannot generate code for it. --autoconf keeps the configuration of the compilers that
  gprbuild looks for on every call in <workspace>/build/gprbuild.cgpr (delete it when the
  toolchain changes): each check is about half the time;
- the language server opens that project, with those variables, instead of the minimal one
  atlas writes.

No dependency: a tolerant parser of the usual subset of GPR, not gprbuild. It understands
`with "a.gpr";` (and limited with), `project X [extends "b.gpr"] is ... end X;` (abstract,
library, aggregate), `type T is ("a", "b");`, variables with `:=` (typed or not),
`external ("X", "default")`, `external_as_list`, lists `(...)`, `&`, `case V is when "a" | "b"
=> ... when others => ... end case;`, `for Attribute [("index")] use ...;`, references to
variables (`V`, `Other.V`) and attributes (`Project'Project_Dir`, `Other'Object_Dir`), and
`dir/**` in Source_Dirs (that folder and all below it). It skips the packages (Compiler,
Builder, Naming...) and the attributes it does not use. What it does not understand is a
warning with its file and line, and the rest of the file is still read: Source_Files and
Source_List_File (atlas reads every file of the source folders), a with that is not next to
the project that imports it (an installed library, found by gprbuild in GPR_PROJECT_PATH or
the toolchain: atlas does not read it), and any other statement.

An external takes its value from project_vars, then from the environment (as gprbuild), then
from its default.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

__all__ = ["code_config", "project_file", "read_project"]

log = logging.getLogger("atlas.code.gpr")

TOKEN = re.compile(r'(?P<space>\s+|--[^\n]*)|(?P<str>"(?:[^"\n]|"")*")|(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)'
                   r'|(?P<sym>:=|=>|[();,&|:\'])|(?P<other>.)')
QUALIFIERS = ("abstract", "aggregate", "library", "configuration", "standard")
# attributes whose folders hold what the compiler produces: excluded from the code
OUTPUT_DIRS = ("object_dir", "exec_dir", "library_dir", "library_ali_dir", "library_src_dir")
DIR_ATTRIBUTES = ("source_dirs", "excluded_source_dirs", *OUTPUT_DIRS)
UNSUPPORTED = {"source_files": "atlas reads every file of the source folders",
               "source_list_file": "atlas reads every file of the source folders",
               "excluded_source_files": "atlas reads every file of the source folders",
               "excluded_source_list_file": "atlas reads every file of the source folders"}


class _Stop(Exception):
    """The file cannot be read further."""


@dataclass
class _Project:
    name: str
    path: Path
    qualifiers: tuple[str, ...] = ()
    vars: dict = field(default_factory=dict)            # lower-case name -> str | list
    types: dict = field(default_factory=dict)           # lower-case name -> allowed values
    attrs: dict = field(default_factory=dict)           # lower-case name (with "(index)") -> str | list
    imports: list = field(default_factory=list)         # _Project of with, extends and Project_Files

    @property
    def dir(self) -> Path:
        return self.path.parent


class _Tokens:
    def __init__(self, text: str, path: Path):
        self.path = path
        self.items: list[tuple[str, str, int]] = []
        line = 1
        for m in TOKEN.finditer(text):
            kind = m.lastgroup
            if kind != "space":
                self.items.append((kind, m.group(), line))
            line += m.group().count("\n")
        self.end_line = line
        self.i = 0

    def peek(self, k: int = 0) -> tuple[str, str, int]:
        j = self.i + k
        return self.items[j] if j < len(self.items) else ("eof", "", self.end_line)

    def next(self) -> tuple[str, str, int]:
        tok = self.peek()
        self.i += 1
        return tok

    def at(self, word: str, k: int = 0) -> bool:
        kind, value, _ = self.peek(k)
        return kind in ("name", "sym") and value.lower() == word

    def accept(self, word: str) -> bool:
        if self.at(word):
            self.i += 1
            return True
        return False

    def expect(self, word: str) -> None:
        if not self.accept(word):
            _, value, line = self.peek()
            raise _Stop(f"{self.path.name}:{line}: expected {word!r}, found {value or 'the end of the file'!r}")

    def name(self) -> str:
        kind, value, line = self.next()
        if kind != "name":
            raise _Stop(f"{self.path.name}:{line}: expected a name, found {value or 'the end of the file'!r}")
        return value

    def string(self) -> str:
        kind, value, line = self.next()
        if kind != "str":
            raise _Stop(f"{self.path.name}:{line}: expected a string, found {value or 'the end of the file'!r}")
        return value[1:-1].replace('""', '"')

    def skip_statement(self) -> None:
        """Up to the next ';' outside parentheses, included."""
        depth = 0
        while True:
            kind, value, _ = self.next()
            if kind == "eof":
                return
            depth += {"(": 1, ")": -1}.get(value, 0)
            if value == ";" and depth <= 0:
                return


def _concat(left, right):
    if isinstance(left, list):
        return left + (right if isinstance(right, list) else [right])
    if isinstance(right, list):
        return [left, *right]
    return left + right


class _Reader:
    def __init__(self, vars: Mapping[str, str], env: Mapping[str, str]):
        self.vars, self.env = dict(vars), env
        self.projects: dict[Path, _Project | None] = {}       # None while it is being read: a cycle
        self.warnings: list[str] = []
        self.externals: dict[str, str] = {}                   # the externals read and their values

    def warn(self, where: str, message: str) -> None:
        text = f"{where}: {message}"
        if text not in self.warnings:
            self.warnings.append(text)

    # --- files --------------------------------------------------------------------------------
    def load(self, path: Path, where: str = "") -> _Project | None:
        path = path.resolve()
        if path in self.projects:
            return self.projects[path]                           # read already, or a limited with
        if not path.is_file():
            self.warn(where or path.name, f"{path} does not exist")
            return None
        self.projects[path] = None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self.warn(where or path.name, f"{path}: {e}")
            return None
        tokens = _Tokens(text, path)
        project = None
        try:
            imports = self.withs(tokens)
            qualifiers = []
            while tokens.peek()[0] == "name" and tokens.peek()[1].lower() in QUALIFIERS:
                qualifiers.append(tokens.next()[1].lower())
            tokens.expect("project")
            project = _Project(tokens.name(), path, tuple(qualifiers), imports=imports)
            self.projects[path] = project
            if tokens.accept("extends"):
                tokens.accept("all")
                line = tokens.peek()[2]
                base = self.load(self.locate(tokens.string(), path), f"{path.name}:{line}")
                if base is not None:
                    project.imports.append(base)
            tokens.expect("is")
            self.declarations(tokens, project, True)
            tokens.expect("end")
        except _Stop as e:
            self.warn(path.name, f"{e}; the rest of the file is not read")
        if project is not None:
            self.projects[path] = project
            for name in project.attrs.get("project_files", []) if "aggregate" in project.qualifiers else []:
                imported = self.load(self.locate(name, path), path.name)
                if imported is not None:
                    project.imports.append(imported)
        return project

    def locate(self, name: str, importer: Path) -> Path:
        p = Path(name if name.lower().endswith(".gpr") else name + ".gpr").expanduser()
        return p if p.is_absolute() else importer.parent / p

    def withs(self, tokens: _Tokens) -> list[_Project]:
        out = []
        while tokens.at("with") or (tokens.at("limited") and tokens.at("with", 1)):
            tokens.accept("limited")
            tokens.expect("with")
            while True:
                line = tokens.peek()[2]
                name = tokens.string()
                where = f"{tokens.path.name}:{line}"
                path = self.locate(name, tokens.path)
                if not path.is_file():
                    self.warn(where, f'with "{name}": not next to {tokens.path.name} (an installed library? atlas '
                                     "does not read GPR_PROJECT_PATH nor the toolchain); its sources are not code roots")
                else:
                    project = self.load(path, where)
                    if project is not None:
                        out.append(project)
                if not tokens.accept(","):
                    break
            tokens.expect(";")
        return out

    # --- declarations -------------------------------------------------------------------------
    def declarations(self, tokens: _Tokens, project: _Project, active: bool) -> None:
        """Until `end` or `when` (not consumed). Only an active part (the chosen branch of a
        case) assigns anything."""
        while True:
            kind, value, line = tokens.peek()
            word = value.lower()
            where = f"{tokens.path.name}:{line}"
            if kind == "eof":
                raise _Stop(f"{where}: the file ends before 'end {project.name};'")
            if word in ("end", "when"):
                return
            if word == "null":
                tokens.next(); tokens.expect(";")
            elif word == "type":
                tokens.next()
                name = tokens.name()
                tokens.expect("is"); tokens.expect("(")
                values = [tokens.string()]
                while tokens.accept(","):
                    values.append(tokens.string())
                tokens.expect(")"); tokens.expect(";")
                if active:
                    project.types[name.lower()] = values
            elif word == "for":
                tokens.next()
                name = tokens.name().lower()
                if tokens.accept("("):
                    name += f"({tokens.string().lower()})"
                    tokens.expect(")")
                tokens.expect("use")
                found = self.expression(tokens, project, active)
                tokens.expect(";")
                if active:
                    project.attrs[name] = found
                    if name in UNSUPPORTED:
                        self.warn(where, f"{name.title()} is not used: {UNSUPPORTED[name]}")
            elif word == "case":
                tokens.next()
                self.case(tokens, project, active, where)
            elif word == "package":
                tokens.next()
                name = tokens.name()
                if tokens.accept("renames"):
                    tokens.name(); tokens.expect(";")
                    continue
                if tokens.accept("extends"):
                    tokens.name()
                tokens.expect("is")
                while not (tokens.at("end") and tokens.peek(1)[1].lower() == name.lower() and tokens.at(";", 2)):
                    if tokens.next()[0] == "eof":
                        raise _Stop(f"{where}: package {name} without its end")
                tokens.next(); tokens.next(); tokens.next()
            elif kind == "name" and (tokens.at(":=", 1) or tokens.at(":", 1)):
                tokens.next()
                kind_of = ""
                if tokens.accept(":"):
                    kind_of = tokens.name()
                tokens.expect(":=")
                found = self.expression(tokens, project, active)
                tokens.expect(";")
                if active:
                    allowed = self.type_values(project, kind_of) if kind_of else None
                    if allowed is not None and found not in allowed:
                        self.warn(where, f"{value} = {found!r} is not one of {kind_of} ({', '.join(allowed)})")
                    project.vars[value.lower()] = found
            else:
                if active:
                    self.warn(where, f"not understood ({value!r}...); skipped up to its ';'")
                tokens.skip_statement()

    def case(self, tokens: _Tokens, project: _Project, active: bool, where: str) -> None:
        name = tokens.name()
        tokens.expect("is")
        current = self.variable(project, name, where, active)
        chosen = False
        while tokens.accept("when"):
            choices, others = [], False
            while True:
                if tokens.accept("others"):
                    others = True
                else:
                    choices.append(tokens.string())
                if not tokens.accept("|"):
                    break
            tokens.expect("=>")
            take = active and not chosen and (others or current in choices)
            chosen = chosen or take
            self.declarations(tokens, project, take)
        tokens.expect("end"); tokens.expect("case"); tokens.expect(";")

    def type_values(self, project: _Project, name: str) -> list[str] | None:
        owner, _, short = name.lower().rpartition(".")
        target = self.project_named(project, owner) if owner else project
        return target.types.get(short) if target else None

    def project_named(self, project: _Project, name: str) -> _Project | None:
        name = name.lower()
        if name in ("project", project.name.lower()):
            return project
        return next((p for p in project.imports if p.name.lower() == name), None)

    # --- expressions --------------------------------------------------------------------------
    def expression(self, tokens: _Tokens, project: _Project, active: bool):
        found = self.term(tokens, project, active)
        while tokens.accept("&"):
            found = _concat(found, self.term(tokens, project, active))
        return found

    def term(self, tokens: _Tokens, project: _Project, active: bool):
        kind, value, line = tokens.next()
        where = f"{tokens.path.name}:{line}"
        if kind == "str":
            return value[1:-1].replace('""', '"')
        if value == "(":
            items: list[str] = []
            if not tokens.accept(")"):
                while True:
                    item = self.expression(tokens, project, active)
                    items += item if isinstance(item, list) else [item]
                    if not tokens.accept(","):
                        break
                tokens.expect(")")
            return items
        if kind != "name":
            raise _Stop(f"{where}: expected a value, found {value or 'the end of the file'!r}")
        word = value.lower()
        if word in ("external", "external_as_list"):
            tokens.expect("(")
            name = tokens.string()
            default = None
            if tokens.accept(","):
                default = self.expression(tokens, project, active)
            tokens.expect(")")
            found = self.vars.get(name, self.env.get(name))
            if found is None:
                if default is None and word == "external":
                    if active:
                        self.warn(where, f'external ("{name}") has no default and no value: give it in '
                                         "[sources.code.project_vars]")
                    default = ""
                found = default if word == "external" else ""
            if active and word == "external":
                self.externals.setdefault(name, found if isinstance(found, str) else " ".join(found))
            if word == "external_as_list":
                separator = default if isinstance(default, str) and default else ","
                return [x for x in str(found).split(separator) if x]
            return found
        if tokens.accept("'"):
            attribute = tokens.name().lower()
            if tokens.accept("("):
                attribute += f"({tokens.string().lower()})"
                tokens.expect(")")
            return self.attribute(project, value, attribute, where, active)
        return self.variable(project, value, where, active)

    def variable(self, project: _Project, name: str, where: str, active: bool):
        owner, _, short = name.lower().rpartition(".")
        target = self.project_named(project, owner) if owner else project
        if target is not None and short in target.vars:
            return target.vars[short]
        if active:
            self.warn(where, f"{name} is not a variable atlas knows; taken as \"\"")
        return ""

    def attribute(self, project: _Project, owner: str, name: str, where: str, active: bool):
        target = self.project_named(project, owner)
        if target is None:
            if active:
                self.warn(where, f"{owner}'{name}: {owner} is not this project nor one it imports; taken as \"\"")
            return ""
        if name == "project_dir":
            return target.dir.as_posix() + "/"
        if name == "name":
            return target.name
        if name in target.attrs:
            found = target.attrs[name]
            if name in DIR_ATTRIBUTES and target is not project:       # a folder of the other project
                return ([(target.dir / f).as_posix() for f in found] if isinstance(found, list)
                        else (target.dir / found).as_posix())
            return found
        if active:
            self.warn(where, f"{owner}'{name} is not set there; taken as \"\"")
        return ""


def _folders(project: _Project, values, warn) -> list[Path]:
    """The folders of Source_Dirs: `dir/**` is that folder and all below it."""
    out: list[Path] = []
    for value in values if isinstance(values, list) else [values]:
        recursive = value.endswith("**")
        base = (project.dir / (value[:-2].rstrip("/\\") or ".")).resolve() if recursive else (project.dir / value).resolve()
        if not base.is_dir():
            warn(project.path.name, f"the source folder {value} does not exist ({base})")
            continue
        out.append(base)
        if recursive:
            out += sorted(p.resolve() for p in base.rglob("*") if p.is_dir())
    return out


def _dir(project: _Project, name: str) -> Path | None:
    value = project.attrs.get(name)
    if isinstance(value, list):
        value = value[0] if value else None
    return (project.dir / value).resolve() if value else None


def read_project(path: str | Path, vars: Mapping[str, str] | None = None,
                 env: Mapping[str, str] | None = None) -> dict:
    """What a GPR project and the ones it imports say about the code: its source folders
    (each one, `**` expanded), its object folders (object, executable and library ones),
    the projects it imports (with, extends; recursively), the Main of the project and the
    warnings of what was not understood. Paths are absolute. `vars` are the scenario
    variables (-X); without them, the environment and then the default of each external."""
    reader = _Reader(vars or {}, os.environ if env is None else env)
    root = reader.load(Path(path).expanduser())
    out = {"name": "", "path": str(Path(path).expanduser().resolve()), "source_dirs": [], "object_dirs": [],
           "withs": [], "main": [], "externals": {}, "warnings": reader.warnings}
    if root is None:
        return out
    order, seen = [], set()

    def visit(p: _Project) -> None:
        if p.path in seen:
            return
        seen.add(p.path)
        order.append(p)
        for q in p.imports:
            visit(q)

    visit(root)
    source_dirs: list[Path] = []
    object_dirs: list[Path] = []
    for p in order:
        if "abstract" in p.qualifiers or "aggregate" in p.qualifiers or p.attrs.get("source_files") == []:
            sources = []
        else:
            sources = _folders(p, p.attrs.get("source_dirs", ["."]), reader.warn)
            excluded = set(_folders(p, p.attrs.get("excluded_source_dirs", []), reader.warn))
            sources = [s for s in sources if s not in excluded]
        source_dirs += [s for s in dict.fromkeys(sources) if s not in source_dirs]
        obj = _dir(p, "object_dir") or p.dir.resolve()
        outputs = [obj, _dir(p, "exec_dir") or obj, *(d for d in (_dir(p, n) for n in OUTPUT_DIRS[2:]) if d)]
        if "abstract" not in p.qualifiers and "aggregate" not in p.qualifiers:
            object_dirs += [d for d in dict.fromkeys(outputs) if d not in object_dirs]
    main = root.attrs.get("main", [])
    out.update(name=root.name, source_dirs=[str(d) for d in source_dirs], object_dirs=[str(d) for d in object_dirs],
               withs=[str(p.path) for p in order[1:]], main=main if isinstance(main, list) else [main],
               externals=reader.externals)
    return out


# --- [sources.code] ---------------------------------------------------------------------------

_warned: set[str] = set()


def project_file(cfg) -> Path | None:
    """The GPR of [sources.code] project, relative to atlas.toml; None without one."""
    sc = cfg["sources"].get("code") or {}
    if not sc.get("project"):
        return None
    p = Path(sc["project"]).expanduser()
    return (p if p.is_absolute() else cfg.path.parent / p).resolve()


def _shown(path: str | Path) -> str:
    """A folder as a root is written by hand: from the working folder when it is inside it."""
    p = Path(path)
    try:
        return p.relative_to(Path.cwd().resolve()).as_posix() or "."
    except ValueError:
        return p.as_posix()


def _outermost(folders: list[str]) -> list[str]:
    """The folders that are not inside another one: the roots are searched recursively."""
    paths = [Path(f) for f in folders]
    return [str(p) for p in paths if not any(q != p and q in p.parents for q in paths)]


def code_config(cfg) -> dict:
    """[sources.code] with what its `project` says (see the module); as it is without one."""
    sc = cfg["sources"].get("code") or {}
    path = project_file(cfg)
    if path is None:
        return sc
    project_vars = {str(k): str(v) for k, v in (sc.get("project_vars") or {}).items()}
    info = read_project(path, project_vars)
    for w in info["warnings"]:
        if w not in _warned:
            _warned.add(w)
            log.warning("[sources.code] project %s: %s", path.name, w)
    out = {**sc, "project": str(path), "project_vars": project_vars}
    if not sc.get("roots"):
        out["roots"] = [_shown(d) for d in _outermost(info["source_dirs"])]
    sources = [Path(d) for d in info["source_dirs"]]
    outputs = [Path(d) for d in info["object_dirs"] if not any(Path(d) == s or Path(d) in s.parents for s in sources)]
    patterns = [pat for d in outputs for pat in dict.fromkeys((f"{_shown(d)}/*", f"{d.as_posix()}/*"))]
    out["exclude"] = [*sc.get("exclude", []), *(p for p in patterns if p not in sc.get("exclude", []))]
    scenario = [f"-X{k}={v}" for k, v in project_vars.items()]
    gprbuild = ["gprbuild", "-P", "{project}", *scenario, "--autoconf={build}/gprbuild.cgpr"]
    if "check_command" not in sc:
        out["check_command"] = [*gprbuild, "-c", "-u", "-f", "{file}"]
        out["check_commands"] = {"ads": [*gprbuild, "-c", "-u", "-f", "{file}", "-cargs", "-gnatc"],
                                 **(sc.get("check_commands") or {})}
    if "test_command" not in sc:
        out["test_command"] = [*gprbuild, "{file}", "-o", "{exe}"]
    out.setdefault("language_server_project", str(path))
    out["_project"] = info
    return out
