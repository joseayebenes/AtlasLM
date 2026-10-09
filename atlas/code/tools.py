"""File tools for coding agents: list, search, read and edit the code of the project.

    list_files   files by glob pattern
    grep         regular expression search, with context lines
    read_file    a numbered fragment of a file
    edit_file    replaces an exact text (unique, or every occurrence)
    write_file   creates a file or rewrites it whole

Every path must be inside [sources.code] roots and outside its `exclude` patterns; ".."
or a symbolic link that leaves them is refused. Paths are given and shown relative to the
working directory, like the file paths of the code graph.

Editing follows the rules of the usual coding agents, which also protect a small model
from itself: a file can only be edited or overwritten after reading it, and not if it
changed since (someone else edited it); the text to replace must appear exactly once
unless `replace_all` is asked; when it is not found, the answer shows the most similar
lines. Writes are atomic and keep the encoding (UTF-8, or Latin-1 for old sources), the
line endings (LF or CRLF) and the permissions of the file. `read_only=True` turns edit and
write off.

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
import threading
from pathlib import Path
from typing import Any, Iterable

from atlas.plugins import Tool, register

__all__ = ["CodeTools", "TOOL_NAMES"]

TOOL_NAMES = ("list_files", "grep", "read_file", "edit_file", "write_file")

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
                 read_only: bool = False):
        self.base = Path(base).resolve()
        self.roots = [(self.base / r).resolve() for r in roots]
        self.exclude = list(exclude)
        self.read_only = read_only
        self._seen: dict[Path, str] = {}    # file -> hash of its content when last read or written here
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg, read_only: bool = False) -> "CodeTools":
        """Over the roots and exclude patterns of [sources.code]; paths from the working directory."""
        sc = cfg["sources"].get("code") or {}
        return cls(sc.get("roots", []), sc.get("exclude", []), read_only=read_only)

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

    def _excluded(self, p: Path) -> bool:
        shown = self._show(p)
        return any(fnmatch.fnmatch(shown, pat) or fnmatch.fnmatch(p.as_posix(), pat) for pat in self.exclude)

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
        A file must be read before it is edited."""
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
        if self.read_only:
            return {"error": "these tools are read-only"}
        p, err = self._resolve(path)
        if err:
            return err
        if not p.is_file():
            return {"error": f"{path} does not exist: create it with write_file"}
        old_text, new_text = old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n")
        if not old_text:
            return {"error": "old_text is empty"}
        if old_text == new_text:
            return {"error": "old_text and new_text are the same"}
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
                return self._not_found(path, text, old_text)
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

    def _not_found(self, path: str, text: str, old_text: str) -> dict:
        lines = text.split("\n")
        first = next((l.strip() for l in old_text.split("\n") if l.strip()), "")
        stripped = [l.strip() for l in lines]
        similar = [i for i, l in enumerate(stripped) if first and l == first]
        if not similar:                         # the most similar lines first
            close = difflib.get_close_matches(first, stripped, n=3, cutoff=0.6)
            similar = list(dict.fromkeys(i for c in close for i, l in enumerate(stripped) if l == c))
        return {"error": f"old_text is not in {path}: copy it exactly (indentation included) from read_file",
                "similar_lines": [{"line": i + 1, "text": lines[i][:MAX_LINE_CHARS]} for i in similar[:5]]}

    def write_file(self, path: str, content: str) -> dict:
        """Creates a file with `content`, or rewrites a file read before. For a small change
        to an existing file use edit_file instead."""
        if self.read_only:
            return {"error": "these tools are read-only"}
        p, err = self._resolve(path)
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
            try:
                out = (content.replace("\n", "\r\n") if crlf else content).encode(encoding)
            except UnicodeEncodeError as e:
                return {"error": f"the file is {encoding} and content has characters it cannot hold: {e}"}
            p.parent.mkdir(parents=True, exist_ok=True)
            created = not p.exists()
            self._write(p, out)
        return {"file": self._show(p), "created": created, "lines": content.count("\n") + 1,
                **({} if created else {"diff": self._diff(self._show(p), before, content)})}


ESCAPES_NOTE = "the backslashes before quotes or n/t in old_text and new_text were read as plain characters"
INDENT_NOTE = "old_text was found ignoring indentation, and new_text was re-indented like the file"


def _unescape(s: str) -> str:
    """What a model meant when it escaped text that is not in a string: \\" -> ", \\n -> newline."""
    return re.sub(r'\\(["\'\\nt])', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


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
    get them wrong, with their escapes undone and ignoring indentation (whole lines)."""
    tries = [(old, new, [])]
    if "\\" in old + new and (_unescape(old), _unescape(new)) != (old, new):
        tries.append((_unescape(old), _unescape(new), [ESCAPES_NOTE]))
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


def _plugin(method: str) -> type:
    doc = (getattr(CodeTools, method).__doc__ or "").strip()

    class CodeTool(Tool):
        name = method
        description = doc

        def __init__(self, context: Any):
            super().__init__(context)
            self.tools = CodeTools.for_context(context)

        def function(self):
            return getattr(self.tools, method)

        def run(self, **kwargs) -> Any:
            return self.function()(**kwargs)

    CodeTool.__name__ = CodeTool.__qualname__ = "".join(w.title() for w in method.split("_")) + "Tool"
    return CodeTool


for _name in TOOL_NAMES:
    register("tool", _name, _plugin(_name))
