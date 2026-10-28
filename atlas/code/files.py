"""Reading the code of the project, for agents and editors: list, search and read files.

    list_files          files by glob pattern
    grep                regular expression search, with context lines
    read_file           a numbered fragment of a file
    outline             the subprograms of a file, with their lines and requirements, and its imports
    read_subprogram     one subprogram, numbered, with its comment (Ada, Python)

Every path must be inside [sources.code] roots and outside its `exclude` patterns; ".."
or a symbolic link that leaves them is refused. Paths are given and shown relative to the
folder of atlas.toml, like the file paths of the code graph, wherever atlas runs from. The
files of its `protected` patterns can be read like the others (hephesto does not let an agent
change them).

outline and read_subprogram need nothing but the file: outline says what it has, and
read_subprogram reads the one to change; read_file reads at most 300 lines, and a small
model's context is small.

Every answer is a compact JSON-serialisable dict; a problem is an answer with `error`,
never an exception, so that the agent can correct itself. When what grep or read_file returns
has a phrase written to steer a model ("ignore previous instructions"), the answer says where,
in `warnings` (atlas/agents/guard.py), and is the same otherwise.

    reader = CodeReader.from_config(cfg)
    reader.grep(r"Delete_Track", glob="*.adb")
    reader.read_file("src/tracks.adb", 10, 20)

ADK takes the bound methods as tools as they are (typed signatures and docstrings). They are
also registered as "tool" plugins with the same names, for [agents.*] tools and [mcp] tools.
Changing files is hephesto's (hephesto.edit.CodeTools, a CodeReader that also writes): what a
read here records is what lets it change a file afterwards.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import re
import threading
from pathlib import Path
from typing import Any, Iterable

from atlas.agents import guard
from atlas.code.common import extract_requirements, requirement_pattern
from atlas.code.symbols import Span, comment_mark, language, subprograms
from atlas.plugins import register_methods

__all__ = ["CodeReader", "READ_TOOL_NAMES", "rebased"]

READ_TOOL_NAMES = ("list_files", "grep", "read_file", "outline", "read_subprogram")

MAX_LINES = 300            # lines of a read
MAX_FILES = 200            # files of a listing
MAX_MATCHES = 200          # matches of a search
MAX_LINE_CHARS = 300       # characters of a line in a search result
MAX_SEARCH_BYTES = 2_000_000   # larger files are not searched
MAX_IMPORTS = 50           # with clauses or imports of an outline
# "with Ada.Text_IO;" (not "with function" of a generic formal), "import os", "from x import y"
IMPORT = {"ada": re.compile(r"(?i)^\s*(?:limited\s+)?(?:private\s+)?with\s+(?!function\b|procedure\b|package\b)[\w.]"),
          "python": re.compile(r"^(?:import|from)\s+\S")}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode(data: bytes) -> tuple[str, str]:
    """(text, encoding): UTF-8, or Latin-1, which decodes anything (old Ada sources)."""
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


class CodeReader:
    def __init__(self, roots: Iterable[str | Path], exclude: Iterable[str] = (), base: str | Path = ".",
                 protected: Iterable[str] = (), requirements: re.Pattern | None = None):
        self.base = Path(base).resolve()
        self.roots = [(self.base / r).resolve() for r in roots]
        self.exclude = list(exclude)
        self.protected = list(protected)
        self._seen: dict[Path, str] = {}    # file -> hash of its content when last read or written here
        self._outlined: dict[Path, str] = {}   # the same, by outline: enough for the subprogram tools
        self.requirements = requirements or requirement_pattern({})      # the tag of [sources.code], for outline
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg, base: str | Path | None = None) -> "CodeReader":
        """Over the roots and exclude patterns of [sources.code] (with a GPR project, those it
        says); paths from the folder of atlas.toml (the working folder), or from `base`, a copy
        of it (a parallel attempt of hephesto develop)."""
        from atlas.code.gpr import code_config
        sc = code_config(cfg)
        roots = [str(cfg.resolve(r)) if base is None else rebased(r, base, cfg.home) for r in sc.get("roots", [])]
        return cls(roots, sc.get("exclude", []), base=cfg.home if base is None else base,
                   protected=sc.get("protected", []), requirements=requirement_pattern(sc))

    @classmethod
    def for_context(cls, context: Any) -> "CodeReader":
        """One instance per context, shared by every tool plugin (and by the CodeTools of
        hephesto, which takes over what was read here)."""
        tools = getattr(context, "_code_tools", None)
        if tools is None:
            tools = cls.from_config(context.cfg)
            context._code_tools = tools
        return tools

    # --- paths ----------------------------------------------------------------
    def _show(self, p: Path) -> str:
        try:
            return p.relative_to(self.base).as_posix()
        except ValueError:
            return p.as_posix()

    def _matches(self, p: Path, patterns: list[str]) -> bool:
        shown = self._show(p)
        return any(fnmatch.fnmatch(shown, pat) or fnmatch.fnmatch(p.as_posix(), pat) for pat in patterns)

    def _excluded(self, p: Path) -> bool:
        return self._matches(p, self.exclude)

    def protected_file(self, p: Path) -> bool:
        """Whether a file is one of [sources.code] protected: it can be read, not changed."""
        return self._matches(p, self.protected)

    def _resolve(self, path: str) -> tuple[Path | None, dict | None]:
        if not self.roots:
            return None, {"error": "there are no code roots ([sources.code] roots)"}
        p = Path(path)
        p = (p if p.is_absolute() else self.base / p).resolve()     # resolves ".." and symbolic links
        if not any(p == r or r in p.parents for r in self.roots):
            return None, {"error": f"{path} is outside the code roots: "
                                   f"{', '.join(self._show(r) for r in self.roots)}"}
        if self._excluded(p):
            return None, {"error": f"{path} is excluded by [sources.code] exclude"}
        return p, None

    def _files(self, where: str, pattern: str) -> tuple[list[Path], dict | None]:
        """Files under `where`: a folder or a file inside the roots, a folder that holds
        some roots (".": those roots), or "" (every root). A pattern without "/"
        ("*.adb") is looked for in every subfolder."""
        if "/" not in pattern and not pattern.startswith("**"):
            pattern = f"**/{pattern}"
        bases = self.roots
        if where:
            q = Path(where)
            q = (q if q.is_absolute() else self.base / q).resolve()
            inside = [r for r in self.roots if r == q or q in r.parents]
            if inside:
                bases = inside
            else:
                p, err = self._resolve(where)
                if err:
                    return [], err
                if p.is_file():
                    return [p], None
                if not p.is_dir():
                    return [], {"error": f"{where} does not exist"}
                bases = [p]
        found = {f.resolve() for b in bases for f in b.glob(pattern) if f.is_file()}
        return sorted(f for f in found if any(r in f.parents for r in self.roots) and not self._excluded(f)), None

    # --- reading --------------------------------------------------------------
    def list_files(self, pattern: str = "**/*", path: str = "", limit: int = MAX_FILES) -> dict:
        """Files of the project that match a glob pattern ("*.adb" in every folder,
        "src/*.ads"), under `path` (a folder; empty = every code root)."""
        files, err = self._files(path, pattern)
        if err:
            return err
        limit = max(1, min(limit, MAX_FILES))
        return {"count": len(files), "files": [self._show(f) for f in files[:limit]], "truncated": len(files) > limit}

    def grep(self, pattern: str, path: str = "", glob: str = "", ignore_case: bool = False, context: int = 0,
             limit: int = 50) -> dict:
        """Lines that match a regular expression, with file and line number, in the files
        under `path` (a folder or a file; empty = every code root) whose name matches
        `glob` ("*.adb"). `context` adds that many lines before and after each match."""
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            return {"error": f"invalid regular expression: {e}"}
        files, err = self._files(path, "**/*")
        if err:
            return err
        limit, context = max(1, min(limit, MAX_MATCHES)), max(0, min(context, 10))
        matches, total = [], 0
        for f in files:
            if glob and not fnmatch.fnmatch(f.name, glob):
                continue
            data = f.read_bytes() if f.stat().st_size <= MAX_SEARCH_BYTES else b""
            if not data or b"\0" in data[:8192]:                # too large or binary
                continue
            lines = _decode(data)[0].splitlines()
            for i, line in enumerate(lines):
                if not rx.search(line):
                    continue
                total += 1
                if len(matches) >= limit:
                    continue
                m = {"file": self._show(f), "line": i + 1, "text": line[:MAX_LINE_CHARS]}
                if context:
                    lo, hi = max(0, i - context), min(len(lines), i + context + 1)
                    m["context"] = "\n".join(f"{k + 1:5d} | {lines[k][:MAX_LINE_CHARS]}" for k in range(lo, hi))
                matches.append(m)
        out = {"count": total, "matches": matches, "truncated": total > len(matches)}
        for m in matches:
            found = guard.find(m.get("context", m["text"]), m["line"])
            if found:
                guard.warn(out, f"{m['file']}:{found[1]}", found[0])
        return out

    def read_file(self, path: str, start: int = 1, end: int = 0) -> dict:
        """Lines `start` to `end` of a file, numbered (end 0 = up to 300 lines from start).
        A file must be read before it is edited. The numbers ("   12 | ") are not part of
        the file: leave them out of the text given to edit_file."""
        p, err = self._resolve(path)
        if err:
            return err
        if not p.is_file():
            return {"error": f"{path} does not exist"}
        data = p.read_bytes()
        lines = _decode(data)[0].splitlines()
        start = max(1, start)
        end = min(len(lines), end if end >= start else start + MAX_LINES - 1, start + MAX_LINES - 1)
        with self._lock:
            self._seen[p] = _sha(data)
        out = {"file": self._show(p), "start": start, "end": end, "total_lines": len(lines),
               "truncated": end < len(lines) and (end - start + 1) >= MAX_LINES,
               "source": "\n".join(f"{i:5d} | {lines[i - 1]}" for i in range(start, end + 1))}
        found = guard.find(out["source"])
        return guard.warn(out, f"{out['file']}:{found[1]}", found[0]) if found else out

    def _source_file(self, path: str, tool: str) -> tuple[Path | None, bytes, str, dict | None]:
        """(path, bytes, text with LF) of an Ada or Python file to read, or why it cannot be."""
        p, err = self._resolve(path)
        if not err and not p.is_file():
            err = {"error": f"{path} does not exist"}
        if not err and language(p.suffix) is None:
            err = {"error": f"{path}: {tool} knows Ada and Python files; use read_file"}
        if err:
            return None, b"", "", err
        data = p.read_bytes()
        return p, data, _decode(data)[0].replace("\r\n", "\n"), None

    def outline(self, path: str) -> dict:
        """The subprograms of a file (Ada, Python), each with its kind (body or declaration),
        its first and last lines, its qualified name, its first line (the signature) and the
        requirement IDs of the comment above it; and the with clauses (imports) of the file.
        To find the subprogram to change without reading the whole file: then read_subprogram.
        After it, replace_subprogram and add_subprogram can change the file."""
        p, data, text, err = self._source_file(path, "outline")
        if err:
            return err
        lines, mark = text.split("\n"), comment_mark(p.suffix)
        out = []
        for s in subprograms(p.suffix, text) or []:
            reqs = extract_requirements("\n".join(lines[_comment_start(lines, s.start, mark) - 1:s.start - 1]),
                                        self.requirements)
            out.append({"kind": s.kind, "start": s.start, "end": s.end, "name": s.qualified,
                        "signature": _signature(lines, s), **({"requirements": reqs} if reqs else {})})
        imports = [l.strip() for l in lines if IMPORT[language(p.suffix)].match(l)]
        with self._lock:
            self._outlined[p] = _sha(data)
        return {"file": self._show(p), "total_lines": len(text.splitlines()), "imports": imports[:MAX_IMPORTS],
                "subprograms": out}

    def read_subprogram(self, path: str, name: str, line: int = 0) -> dict:
        """One subprogram of a file (Ada, Python), numbered, with the comment lines above it.
        `name` can be qualified (Controller.Execute); `line`, a line inside it, picks one when
        several have that name (outline gives the lines). It counts as reading the file:
        replace_subprogram, add_subprogram and edit_file can change it after this. The
        numbers ("   12 | ") are not part of the file."""
        p, data, text, err = self._source_file(path, "read_subprogram")
        if err:
            return err
        spans = subprograms(p.suffix, text) or []
        short, same = name.strip().split(".")[-1], _matcher(p.suffix)
        named = [s for s in spans if same(s.name, short) and _qualified_match(s, name, same)]
        found = sorted((s for s in named if s.start <= line <= s.end), key=lambda s: -s.start)[:1] if line else named
        if len(found) > 1 and sum(s.kind == "body" for s in found) == 1:     # a body and its declaration
            found = [s for s in found if s.kind == "body"]
        where = lambda spans: ", ".join(f"{s.qualified} (lines {s.start}-{s.end})" for s in spans)
        if not found and named:
            return {"error": f"none of the subprograms named {short} in {path} has the line {line}: {where(named)}"}
        if not found:
            return _missing(path, short, "", [], spans)
        if len(found) > 1:
            return {"error": f"{len(found)} subprograms are named {short} in {path}: {where(found)}. Give `line`, a "
                             "line inside the one to read"}
        target, lines = found[0], text.split("\n")
        start = _comment_start(lines, target.start, comment_mark(p.suffix), blank=False)
        end = min(target.end, start + MAX_LINES - 1)
        with self._lock:
            self._seen[p] = _sha(data)
        return {"file": self._show(p), "name": target.qualified, "kind": target.kind, "start": start, "end": end,
                "total_lines": len(text.splitlines()), **({"truncated": True} if end < target.end else {}),
                "source": "\n".join(f"{i:5d} | {lines[i - 1]}" for i in range(start, end + 1))}


def rebased(path: str, base: str | Path, home: str | Path) -> str:
    """A path of the configuration in `base`, a copy of the working folder `home` (the folder
    of atlas.toml): a relative one is relative to base now, an absolute one inside home moves
    into base; one outside it is the same for every copy."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        return str(Path(base) / p)
    try:
        return str(Path(base) / p.resolve().relative_to(Path(home).resolve()))
    except ValueError:
        return path


def _matcher(suffix: str):
    """How names compare in a language: Ada does not tell case apart."""
    if language(suffix) == "ada":
        return lambda a, b: a.lower() == b.lower()
    return lambda a, b: a == b


def _qualified_match(span: Span, name: str, same) -> bool:
    """Whether a qualified name (Controller.Execute, C.m) fits the place of a span: its parts
    before the last one are the last of the span's parents."""
    parts = name.strip().split(".")[:-1]
    if not parts:
        return True
    parents = span.parents[-len(parts):]
    return len(parents) == len(parts) and all(same(a, b) for a, b in zip(parents, parts))


def _missing(path: str, name: str, kind: str, named: list[Span], spans: list[Span]) -> dict:
    """Why no subprogram fits, with what the file has."""
    if named:
        other = named[0]
        return {"error": f"{path} has no {kind} of {name}, but it has a {other.kind} (lines {other.start}-"
                         f"{other.end}): give a {other.kind}, or add the {kind} with add_subprogram"}
    names = sorted({s.qualified for s in spans})
    close = difflib.get_close_matches(name.lower(), [s.name.lower() for s in spans], n=3, cutoff=0.6)
    return {"error": f"{path} has no subprogram named {name}"
                     + (f" (similar: {', '.join(close)})" if close else "")
                     + (f". Its subprograms: {', '.join(names[:40])}" if names else ". It has no subprograms")
                     + (". To add a new one, use add_subprogram" if kind else "")}


def _comment_start(lines: list[str], start: int, mark: str, blank: bool = True) -> int:
    """The first line of the comment right above line `start` (1-based; `start` if there is
    none). With `blank`, one blank line between the comment and the code is tolerated, as the
    analyzers of the code do."""
    above = start - 1                    # the number of the line above
    if blank and above >= 2 and not lines[above - 1].strip() and lines[above - 2].strip().startswith(mark):
        above -= 1
    first = start
    while above >= 1 and lines[above - 1].strip().startswith(mark):
        first, above = above, above - 1
    return first


def _signature(lines: list[str], span: Span) -> str:
    """The first line of a subprogram that says what it is: not a decorator, not `overriding`."""
    for line in lines[span.start - 1:span.end]:
        s = line.strip()
        if s and not s.startswith("@") and s.lower() not in ("overriding", "not overriding"):
            return s[:MAX_LINE_CHARS]
    return lines[span.start - 1].strip()[:MAX_LINE_CHARS]


register_methods(CodeReader, READ_TOOL_NAMES)
