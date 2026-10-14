"""File tools for coding agents: list, search, read and edit the code of the project.

    list_files          files by glob pattern
    grep                regular expression search, with context lines
    read_file           a numbered fragment of a file
    edit_file           replaces an exact text (unique, or every occurrence)
    write_file          creates a file or rewrites it whole
    replace_subprogram  replaces a whole subprogram, found by its name (Ada, Python)
    add_subprogram      adds subprograms after another one or at the end of the declarations

Every path must be inside [sources.code] roots and outside its `exclude` patterns; ".."
or a symbolic link that leaves them is refused. Paths are given and shown relative to the
working directory, like the file paths of the code graph. The files of its `protected`
patterns can be read but not edited or written: the tests, so that the agent that writes the
code cannot change what judges it (both gemma-4-26b and devstral, asked to fix a failing test,
changed what it expected instead of the code).

Editing follows the rules of the usual coding agents, which also protect a small model
from itself: a file can only be edited or overwritten after reading it, and not if it
changed since (someone else edited it); the text to replace must appear exactly once
unless `replace_all` is asked; when it is not found, the answer shows the most similar
lines. Writes are atomic and keep the encoding (UTF-8, or Latin-1 for old sources), the
line endings (LF or CRLF) and the permissions of the file. `read_only=True` turns edit and
write off.

replace_subprogram and add_subprogram need nothing copied from the file: the agent gives the
whole subprogram, and the file says where it goes (symbols.py). gemma-4-26b tried to replace
whole blocks with edit_file: 39 of its 151 edits in a run of atlas develop were refused for an
old_text of more than 30 lines, and 34 more did not match the file.

Every answer is a compact JSON-serialisable dict; a problem is an answer with `error`,
never an exception, so that the agent can correct itself.

    tools = CodeTools.from_config(cfg)
    tools.grep(r"Delete_Track", glob="*.adb")
    tools.read_file("src/tracks.adb", 10, 20)
    tools.edit_file("src/tracks.adb", 'Logger.Log ("delete");', 'Logger.Log ("track deleted");')

ADK takes the bound methods as tools as they are (typed signatures and docstrings):
LlmAgent(..., tools=[tools.grep, tools.read_file, tools.edit_file]). They are also
registered as "tool" plugins with the same names, for [agents.*] tools and [mcp] tools.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import re
import shutil
import textwrap
import threading
from pathlib import Path
from typing import Any, Iterable

from atlas.code.symbols import Span, comment_mark, insertion_line, language, new_subprograms, subprograms
from atlas.plugins import register_methods

__all__ = ["CodeTools", "TOOL_NAMES", "WRITE_TOOLS"]

TOOL_NAMES = ("list_files", "grep", "read_file", "edit_file", "write_file", "replace_subprogram", "add_subprogram")
WRITE_TOOLS = ("edit_file", "write_file", "replace_subprogram", "add_subprogram")

MAX_LINES = 300            # lines of a read
MAX_FILES = 200            # files of a listing
MAX_MATCHES = 200          # matches of a search
MAX_LINE_CHARS = 300       # characters of a line in a search result
MAX_DIFF_CHARS = 4000      # characters of the diff of an edit
MAX_SEARCH_BYTES = 2_000_000   # larger files are not searched


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode(data: bytes) -> tuple[str, str]:
    """(text, encoding): UTF-8, or Latin-1, which decodes anything (old Ada sources)."""
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


class CodeTools:
    def __init__(self, roots: Iterable[str | Path], exclude: Iterable[str] = (), base: str | Path = ".",
                 read_only: bool = False, protected: Iterable[str] = ()):
        self.base = Path(base).resolve()
        self.roots = [(self.base / r).resolve() for r in roots]
        self.exclude = list(exclude)
        self.protected = list(protected)
        self.read_only = read_only
        self._seen: dict[Path, str] = {}    # file -> hash of its content when last read or written here
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg, read_only: bool = False) -> "CodeTools":
        """Over the roots and exclude patterns of [sources.code]; paths from the working directory."""
        sc = cfg["sources"].get("code") or {}
        return cls(sc.get("roots", []), sc.get("exclude", []), read_only=read_only, protected=sc.get("protected", []))

    @classmethod
    def for_context(cls, context: Any) -> "CodeTools":
        """One instance per context, shared by every tool plugin: what read_file reads is
        what edit_file can edit."""
        tools = getattr(context, "_code_tools", None)
        if tools is None:
            tools = cls.from_config(context.cfg)
            setattr(context, "_code_tools", tools)
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

    def _writable(self, path: str) -> tuple[Path | None, dict | None]:
        """The path of a file to change, or why it cannot be changed."""
        if self.read_only:
            return None, {"error": "these tools are read-only"}
        p, err = self._resolve(path)
        if err:
            return None, err
        if self.protected_file(p):
            return None, {"error": f"{path} is protected ([sources.code] protected): it can be read, not changed. "
                                   "It checks the code, so when it fails, change the code it checks"}
        return p, None

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
        return {"count": total, "matches": matches, "truncated": total > len(matches)}

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
        return {"file": self._show(p), "start": start, "end": end, "total_lines": len(lines),
                "truncated": end < len(lines) and (end - start + 1) >= MAX_LINES,
                "source": "\n".join(f"{i:5d} | {lines[i - 1]}" for i in range(start, end + 1))}

    # --- editing --------------------------------------------------------------
    def _check_seen(self, p: Path, path: str, data: bytes) -> dict | None:
        seen = self._seen.get(p)
        if seen is None:
            return {"error": f"read {path} with read_file before changing it"}
        if seen != _sha(data):
            return {"error": f"{path} changed since you read it: read it again"}
        return None

    def _write(self, p: Path, data: bytes) -> None:
        """Atomic: a temporary file next to it and a rename, keeping the permissions."""
        tmp = p.with_name(f".{p.name}.atlas-tmp")
        tmp.write_bytes(data)
        if p.exists():
            shutil.copymode(p, tmp)
        os.replace(tmp, p)
        self._seen[p] = _sha(data)

    def _diff(self, path: str, before: str, after: str) -> str:
        diff = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                            fromfile=path, tofile=path, n=2))
        return diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + "\n… (diff truncated)"

    def edit_file(self, path: str, old_text: str, new_text: str, replace_all: bool = False) -> dict:
        """Replaces `old_text` by `new_text` in a file read before. `old_text` must be copied
        from the file (without the line numbers of read_file) and appear once; with
        replace_all every occurrence is replaced. Returns the diff: check it."""
        p, err = self._writable(path)
        if err:
            return err
        if not p.is_file():
            return {"error": f"{path} does not exist: create it with write_file"}
        old_text, new_text = old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n")
        if not old_text:
            return {"error": "old_text is empty"}
        if old_text == new_text:
            return {"error": "old_text and new_text are the same"}
        size = old_text.strip("\n").count("\n") + 1
        if size > MAX_OLD_LINES:
            return {"error": f"old_text has {size} lines: copy at most {MAX_OLD_LINES} lines from read_file and make "
                             "the change in several edits (or rewrite the whole file with write_file)"}
        with self._lock:
            data = p.read_bytes()
            err = self._check_seen(p, path, data)
            if err:
                return err
            raw, encoding = _decode(data)
            crlf = "\r\n" in raw
            text = raw.replace("\r\n", "\n")
            found = _find(text, old_text, new_text, replace_all)
            if found is None:
                return self._not_found(path, text, old_text, new_text)
            if "error" in found:
                return found
            new, count, notes = found["text"], found["count"], found["notes"]
            try:
                out = (new.replace("\n", "\r\n") if crlf else new).encode(encoding)
            except UnicodeEncodeError as e:
                return {"error": f"the file is {encoding} and new_text has characters it cannot hold: {e}"}
            self._write(p, out)
        return {"file": self._show(p), "replacements": count, "diff": self._diff(self._show(p), text, new),
                **({"notes": notes} if notes else {})}

    def _not_found(self, path: str, text: str, old_text: str, new_text: str) -> dict:
        """Why old_text was not found, with what helps a model try again: whether new_text is
        already in the file (a model that does not notice its edit was made repeats it: 8 of
        11 failed edits of gemma-4-26b in one task), the block of the file most like old_text,
        and the lines like its first one."""
        lines = text.split("\n")
        squash = lambda s: re.sub(r"\s+", " ", LISTING.sub("", s)).strip()
        new = squash(new_text)
        if new and new in squash(text):         # where: by its most distinctive (longest) line
            new_lines = [l.strip() for l in LISTING.sub("", new_text).strip("\n").split("\n")]
            k = max(range(len(new_lines)), key=lambda j: len(new_lines[j]))
            at = next((i + 1 - k for i, l in enumerate(lines) if l.strip() == new_lines[k]), None)
            return {"error": f"old_text is not in {path}, but new_text already is"
                             + (f" (line {at})" if at else "") + ": this change is already made. Read the file "
                             "again to see its current text before another edit", **({"line": at} if at else {})}
        first = next((l.strip() for l in old_text.split("\n") if l.strip()), "")
        stripped = [l.strip() for l in lines]
        similar = [i for i, l in enumerate(stripped) if first and l == first]
        if not similar:                         # the most similar lines first
            close = difflib.get_close_matches(first, stripped, n=3, cutoff=0.6)
            similar = list(dict.fromkeys(i for c in close for i, l in enumerate(stripped) if l == c))
        out = {"error": f"old_text is not in {path}: copy it exactly (indentation included) from read_file",
               "similar_lines": [{"line": i + 1, "text": lines[i][:MAX_LINE_CHARS]} for i in similar[:5]]}
        size, old = len(old_text.strip("\n").split("\n")), squash(old_text)
        if old and len(lines) <= MAX_CLOSEST_LINES and size <= MAX_CLOSEST_BLOCK:
            ratio, start = max((difflib.SequenceMatcher(None, squash("\n".join(lines[i:i + size])), old).ratio(), i)
                               for i in range(max(1, len(lines) - size + 1)))
            if ratio >= 0.5:                    # what the file has now where old_text should be
                out["closest"] = {"start_line": start + 1, "end_line": min(len(lines), start + size),
                                  "text": "\n".join(lines[start:start + size])}
        return out

    def write_file(self, path: str, content: str) -> dict:
        """Creates a file with `content`, or rewrites a file read before. For a small change
        to an existing file use edit_file instead."""
        p, err = self._writable(path)
        if err:
            return err
        if p.is_dir():
            return {"error": f"{path} is a folder"}
        with self._lock:
            before, encoding, crlf = "", "utf-8", False
            if p.exists():
                data = p.read_bytes()
                err = self._check_seen(p, path, data)
                if err:
                    return err
                raw, encoding = _decode(data)
                before, crlf = raw.replace("\r\n", "\n"), "\r\n" in raw
            content = content.replace("\r\n", "\n")
            unlisted = _line_backslashes(content)
            if unlisted:
                content = content.replace("\\\n", "\n")
            try:
                out = (content.replace("\n", "\r\n") if crlf else content).encode(encoding)
            except UnicodeEncodeError as e:
                return {"error": f"the file is {encoding} and content has characters it cannot hold: {e}"}
            p.parent.mkdir(parents=True, exist_ok=True)
            created = not p.exists()
            self._write(p, out)
        if not created and before == content:
            return {"file": self._show(p), "created": False, "unchanged": True, "lines": content.count("\n") + 1,
                    "note": "the file already had exactly this content: nothing changed"}
        return {"file": self._show(p), "created": created, "lines": content.count("\n") + 1,
                **({} if created else {"diff": self._diff(self._show(p), before, content)}),
                **({"notes": [LISTING_NOTE]} if unlisted else {})}

    # --- whole subprograms ----------------------------------------------------
    def _current(self, p: Path, path: str) -> tuple[str, str, bool] | dict:
        """(text with LF, encoding, CRLF?) of a file read before, or why it cannot be changed."""
        data = p.read_bytes()
        err = self._check_seen(p, path, data)
        if err:
            return err
        raw, encoding = _decode(data)
        return raw.replace("\r\n", "\n"), encoding, "\r\n" in raw

    def _store(self, p: Path, text: str, encoding: str, crlf: bool) -> dict | None:
        try:
            out = (text.replace("\n", "\r\n") if crlf else text).encode(encoding)
        except UnicodeEncodeError as e:
            return {"error": f"the file is {encoding} and the new text has characters it cannot hold: {e}"}
        self._write(p, out)
        return None

    def _subprogram_file(self, path: str) -> tuple[Path | None, dict | None]:
        p, err = self._writable(path)
        if err:
            return None, err
        if not p.is_file():
            return None, {"error": f"{path} does not exist: create it with write_file"}
        if language(p.suffix) is None:
            return None, {"error": f"{path}: the subprogram tools know Ada and Python files; use edit_file"}
        return p, None

    def replace_subprogram(self, path: str, name: str, new_text: str, line: int = 0) -> dict:
        """Replaces a whole subprogram of a file read before. `new_text` is the complete new
        subprogram, from `procedure Name` or `function Name` (`def name` in Python) to its end
        (`end Name;`), with its comment lines above when they change too. Better than
        edit_file for any change inside a subprogram: nothing has to be copied from the file.
        `name` can be qualified (Controller.Execute); `line`, a line inside it, picks one when
        several have that name. Returns the diff."""
        p, err = self._subprogram_file(path)
        if err:
            return err
        new_text, notes = _cleaned(new_text)
        short = name.strip().split(".")[-1]
        same = _matcher(p.suffix)
        mine = [s for s in new_subprograms(p.suffix, new_text) if same(s.name, short)]
        if len(mine) != 1:
            what = "two" if mine else "no"
            return {"error": f"new_text has {what} complete subprogram named {short}: give the whole subprogram, "
                             f"from its first line to its end ({'end ' + short + ';' if language(p.suffix) == 'ada' else 'the last line of its body'})"}
        with self._lock:
            current = self._current(p, path)
            if isinstance(current, dict):
                return current
            text, encoding, crlf = current
            spans = subprograms(p.suffix, text) or []
            named = [s for s in spans if same(s.name, short) and _qualified_match(s, name, same)]
            found = [s for s in named if s.kind == mine[0].kind]
            if line:
                found = sorted((s for s in found if s.start <= line <= s.end), key=lambda s: -s.start)[:1]
            if not found:
                return _missing(path, short, mine[0].kind, named, spans)
            if len(found) > 1:
                where = ", ".join(f"{s.qualified} (lines {s.start}-{s.end})" for s in found)
                return {"error": f"{len(found)} subprograms are named {short} in {path}: {where}. Give `line`, a line "
                                 "inside the one to replace"}
            target = found[0]
            lines = text.split("\n")
            start, end = target.start - 1, target.end
            block = _block(new_text, _indent(lines[start]))
            mark = comment_mark(p.suffix)
            if block and block[0].strip().startswith(mark):        # its comment comes too: it replaces the old one
                while start > 0 and lines[start - 1].strip().startswith(mark):
                    start -= 1
            new = "\n".join(lines[:start] + block + lines[end:])
            err = self._store(p, new, encoding, crlf)
            if err:
                return err
        return {"file": self._show(p), "replaced": f"{target.kind} of {target.qualified}, lines {target.start}-"
                                                  f"{target.end}",
                "diff": self._diff(self._show(p), text, new), **({"notes": notes} if notes else {})}

    def add_subprogram(self, path: str, new_text: str, after: str = "") -> dict:
        """Adds new subprograms to a file read before; `new_text` holds them complete. They go
        right after the subprogram named `after`, or else at the end of the declarations of
        the package (before `private` in a spec, before the `begin` or the `end` of a package
        body; at the end of a Python file). Returns the diff."""
        p, err = self._subprogram_file(path)
        if err:
            return err
        new_text, notes = _cleaned(new_text)
        same = _matcher(p.suffix)
        added = new_subprograms(p.suffix, new_text)
        if not added:
            return {"error": "new_text holds no complete subprogram: give each one whole, from its first line to its "
                             "end"}
        with self._lock:
            current = self._current(p, path)
            if isinstance(current, dict):
                return current
            text, encoding, crlf = current
            spans = subprograms(p.suffix, text) or []
            for a in added:
                twin = next((s for s in spans if same(s.name, a.name) and s.kind == a.kind), None)
                if twin:
                    return {"error": f"{path} already has a {twin.kind} of {twin.qualified} (lines {twin.start}-"
                                     f"{twin.end}): change it with replace_subprogram (an overload: edit_file)"}
            lines = text.split("\n")
            if after:
                found = [s for s in spans if same(s.name, after.strip().split(".")[-1])
                         and _qualified_match(s, after, same)]
                bodies = [s for s in found if s.kind == "body"] or found
                if not bodies:
                    return _missing(path, after.strip().split(".")[-1], "", [], spans)
                where, indent = bodies[-1].end + 1, _indent(lines[bodies[-1].start - 1])
            else:
                place = insertion_line(p.suffix, text)
                if place is None:
                    return {"error": f"{path} is not a package: say after which subprogram the new one goes (`after`)"}
                where, indent = place[0], " " * place[1]
            block = _block(new_text, indent)
            gap = [""] * (2 if language(p.suffix) == "python" and not indent else 1)
            head = lines[:where - 1]
            while head and not head[-1].strip() and len(head) > 1:     # one gap before, whatever there was
                head.pop()
            tail = lines[where - 1:]
            new = "\n".join(head + gap + block + ([""] if tail and tail[0].strip() else []) + tail)
            err = self._store(p, new, encoding, crlf)
            if err:
                return err
        return {"file": self._show(p), "added": [a.name for a in added],
                "diff": self._diff(self._show(p), text, new), **({"notes": notes} if notes else {})}


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


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _block(text: str, indent: str) -> list[str]:
    """The lines of a new text, dedented and indented like the place where they go."""
    body = textwrap.dedent(text).strip("\n").split("\n")
    return [indent + l if l.strip() else "" for l in body]


def _cleaned(text: str) -> tuple[str, list[str]]:
    """A new text without the listing of read_file that small models copy into it (line
    numbers, backslashes before line breaks), with a note when there was one. Escapes stay:
    a whole subprogram is not a copied fragment, and "\\n" in a string is code."""
    text = text.replace("\r\n", "\n")
    if LISTING.search(text) or _line_backslashes(text):
        return LISTING.sub("", text.replace("\\\n", "\n")), [LISTING_NOTE]
    return text, []


ESCAPES_NOTE = "the backslashes before quotes or n/t in old_text and new_text were read as plain characters"
INDENT_NOTE = "old_text was found ignoring indentation, and new_text was re-indented like the file"
LISTING_NOTE = ("the line numbers of read_file and the backslashes before line breaks were removed from old_text "
                "and new_text: copy the text without them")
LISTING = re.compile(r"(?m)^[ \t]*\d+ \|(?: |$)")      # "   12 | " at the start of a line, as read_file shows it
MAX_CLOSEST_LINES, MAX_CLOSEST_BLOCK = 5000, 60   # how big a file and an old_text get the closest block
MAX_OLD_LINES = 30           # a longer old_text is rejected: models rebuild it from memory instead of copying it


def _unescape(s: str) -> str:
    """What a model meant when it escaped text that is not in a string: \\" -> ", \\n -> newline."""
    return re.sub(r'\\(["\'\\nt])', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def _unlist(s: str) -> str:
    """What a model meant when it copied the listing of read_file into a text, with the line
    numbers, a backslash before each line break and some line breaks written as \\n."""
    return LISTING.sub("", _unescape(s.replace("\\\n", "\n")))


def _line_backslashes(s: str) -> bool:
    """Whether a model ended (nearly) every line of a text with a backslash, as in the listing
    of read_file (gemma-4-26b in new_text alone, with an old_text copied right)."""
    breaks = s.rstrip("\n").count("\n")                      # the final line break does not count
    return breaks >= 2 and s.count("\\\n") >= 0.8 * breaks


def _occurrences(text: str, old: str, starts: list[int] | None = None) -> list[int]:
    """Line numbers of the occurrences of `old` (or of the given line indexes)."""
    if starts is not None:
        return [i + 1 for i in starts]
    out, pos = [], text.find(old)
    while pos >= 0:
        out.append(text.count("\n", 0, pos) + 1)
        pos = text.find(old, pos + 1)
    return out


def _reindent(new_lines: list[str], old_indent: str, file_indent: str) -> list[str]:
    return [file_indent + l[len(old_indent):] if l.strip() and l.startswith(old_indent) else l for l in new_lines]


def _find(text: str, old: str, new: str, replace_all: bool) -> dict | None:
    """The text with the replacement done: {"text", "count", "notes"}, an error when it is
    ambiguous, or None when old is not there. Exact first; then, as small models often
    get them wrong, with their escapes undone, without the line numbers of read_file they
    copied (gemma4:12b in edits of several lines), and ignoring indentation (whole lines)."""
    first_notes = []
    if _line_backslashes(new) and "\\\n" not in old:
        new, first_notes = new.replace("\\\n", "\n"), [LISTING_NOTE]
    tries = [(old, new, first_notes)]
    if "\\" in old + new and (_unescape(old), _unescape(new)) != (old, new):
        tries.append((_unescape(old), _unescape(new), [ESCAPES_NOTE]))
    if (LISTING.search(old) or "\\\n" in old) and (_unlist(old), _unlist(new)) != (old, new):
        tries.append((_unlist(old), _unlist(new), [LISTING_NOTE]))
    ambiguous = "old_text appears {n} times (lines {lines}): add more surrounding text to make it unique, " \
                "or use replace_all"
    for o, n, notes in tries:                                 # exact
        count = text.count(o)
        if count > 1 and not replace_all:
            return {"error": ambiguous.format(n=count, lines=_occurrences(text, o)[:20])}
        if count:
            return {"text": text.replace(o, n) if replace_all else text.replace(o, n, 1),
                    "count": count, "notes": notes}
    lines = text.split("\n")
    stripped = [l.strip() for l in lines]
    for o, n, notes in tries:                                 # whole lines, ignoring indentation
        old_lines = o.strip("\n").split("\n")
        target = [l.strip() for l in old_lines]
        if not any(target):
            continue
        starts = [i for i in range(len(lines) - len(target) + 1) if stripped[i:i + len(target)] == target]
        if len(starts) > 1 and not replace_all:
            return {"error": ambiguous.format(n=len(starts), lines=_occurrences(text, o, starts)[:20])}
        if not starts:
            continue
        old_indent = next(l for l in old_lines if l.strip())
        old_indent = old_indent[:len(old_indent) - len(old_indent.lstrip())]
        new_lines = n.strip("\n").split("\n") if n.strip("\n") else []
        for i in reversed(starts):                            # from the end: the indexes stay valid
            file_indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
            lines[i:i + len(target)] = _reindent(new_lines, old_indent, file_indent)
        return {"text": "\n".join(lines), "count": len(starts), "notes": notes + [INDENT_NOTE]}
    return None


register_methods(CodeTools, TOOL_NAMES, writes=WRITE_TOOLS)
