"""File tools for coding agents: the reading of CodeReader (atlas/code/files.py), and editing.

    list_files, grep, read_file, outline, read_subprogram    as CodeReader
    edit_file           replaces an exact text (unique, or every occurrence)
    write_file          creates a file or rewrites it whole
    replace_subprogram  replaces a whole subprogram, found by its name (Ada, Python)
    add_subprogram      adds subprograms after another one or at the end of the declarations

Every path must be inside [sources.code] roots and outside its `exclude` patterns; ".."
or a symbolic link that leaves them is refused. Paths are given and shown relative to the
folder of atlas.toml, like the file paths of the code graph, wherever atlas runs from. The files of its `protected`
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

The rules of the project (BuildTools gives them, see build.py) look at the lines a change
adds, in a new file too: one of level error refuses the change, with the line; a warning goes
in the `notes` of the answer.

replace_subprogram and add_subprogram need nothing copied from the file: the agent gives the
whole subprogram, and the file says where it goes (symbols.py). gemma-4-26b tried to replace
whole blocks with edit_file: 39 of its 151 edits in a run of atlas develop were refused for an
old_text of more than 30 lines, and 34 more did not match the file. They do not need the whole
file read either: outline (what the file has) is enough for them, and read_subprogram (the
one to change, which also counts as a read for edit_file); read_file reads at most 300 lines,
and a small model's context is small.

Every answer is a compact JSON-serialisable dict; a problem is an answer with `error`,
never an exception, so that the agent can correct itself. When what grep or read_file returns
has a phrase written to steer a model ("ignore previous instructions"), the answer says where,
in `warnings` (atlas/agents/guard.py), and is the same otherwise.

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
import os
import re
import shutil
import textwrap
from pathlib import Path
from typing import Any, Callable, Iterable

from atlas.code.files import (MAX_LINE_CHARS, READ_TOOL_NAMES, CodeReader, _decode, _matcher, _missing,
                              _qualified_match, _sha, rebased)
from atlas.code.common import requirement_pattern
from atlas.code.symbols import comment_mark, insertion_line, language, new_subprograms, subprograms
from atlas.plugins import register_methods

__all__ = ["CodeTools", "TOOL_NAMES", "WRITE_TOOLS", "rebased"]

WRITE_TOOLS = ("edit_file", "write_file", "replace_subprogram", "add_subprogram")
TOOL_NAMES = (*READ_TOOL_NAMES, *WRITE_TOOLS)

MAX_DIFF_CHARS = 4000      # characters of the diff of an edit


class CodeTools(CodeReader):
    def __init__(self, roots: Iterable[str | Path], exclude: Iterable[str] = (), base: str | Path = ".",
                 read_only: bool = False, protected: Iterable[str] = (), requirements: re.Pattern | None = None):
        super().__init__(roots, exclude, base, protected, requirements)
        self.read_only = read_only
        # Set by BuildTools (see build.py): the syntax errors of a text as the content of a file
        # (None: nothing checks that language), what the rules of the project find in the lines
        # a change adds (file, new text, old text or None), and check_code of a file after a change
        self.syntax: Callable[[Path, str], list[dict] | None] | None = None
        self.rules: Callable[[Path, str, str | None], list[dict]] | None = None
        self.after_change: Callable[[Path], dict | None] | None = None

    @classmethod
    def from_config(cls, cfg, read_only: bool = False, base: str | Path | None = None) -> "CodeTools":  # type: ignore[override]
        """Over the roots and exclude patterns of [sources.code] (with a GPR project, those it
        says); paths from the folder of atlas.toml (the working folder), or from `base`, a copy
        of it (a parallel attempt of atlas develop)."""
        from atlas.code.gpr import code_config
        sc = code_config(cfg)
        roots = [str(cfg.resolve(r)) if base is None else rebased(r, base, cfg.home) for r in sc.get("roots", [])]
        return cls(roots, sc.get("exclude", []), base=cfg.home if base is None else base, read_only=read_only,
                   protected=sc.get("protected", []), requirements=requirement_pattern(sc))

    @classmethod
    def for_context(cls, context: Any) -> "CodeTools":
        """One instance per context, shared by every tool plugin: what read_file reads is
        what edit_file can edit. A CodeReader of the context (the reading tools of atlas, called
        first) gives way to a CodeTools that keeps what it read. Its BuildTools gives it the
        syntax check and check_code after each change, when [sources.code] has them."""
        tools = getattr(context, "_code_tools", None)
        if not isinstance(tools, cls):
            reader, tools = tools, cls.from_config(context.cfg)
            if reader is not None:              # the same reads: a read there allows an edit here
                tools._seen, tools._outlined, tools._lock = reader._seen, reader._outlined, reader._lock
            context._code_tools = tools
            from hephesto.build import BuildTools
            BuildTools.for_context(context)
        return tools

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
    # --- editing --------------------------------------------------------------
    def _check_seen(self, p: Path, path: str, data: bytes, outlined: bool = False) -> dict | None:
        """Why the file cannot be changed: not read, or changed since. `outlined`: outline
        counts as a read (for the subprogram tools)."""
        seen = self._seen.get(p) or (self._outlined.get(p) if outlined else None)
        if seen is None:
            tools = "outline, read_subprogram or read_file" if outlined else "read_file"
            return {"error": f"read {path} with {tools} before changing it"}
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
            stored = self._store(p, new, encoding, crlf, before=text)
            if isinstance(stored, dict):
                return stored
            notes = notes + stored
        return {"file": self._show(p), "replacements": count, "diff": self._diff(self._show(p), text, new),
                **({"notes": notes} if notes else {}), **self._checked(p)}

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
            before, encoding, crlf = None, "utf-8", False
            if p.exists():
                current = self._current(p, path)
                if isinstance(current, dict):
                    return current
                before, encoding, crlf = current
            content = content.replace("\r\n", "\n")
            unlisted = _line_backslashes(content)
            if unlisted:
                content = content.replace("\\\n", "\n")
            if before == content:
                return {"file": self._show(p), "created": False, "unchanged": True, "lines": content.count("\n") + 1,
                        "note": "the file already had exactly this content: nothing changed"}
            stored = self._store(p, content, encoding, crlf, before=before)
            if isinstance(stored, dict):
                return stored
            notes = ([LISTING_NOTE] if unlisted else []) + stored
        return {"file": self._show(p), "created": before is None, "lines": content.count("\n") + 1,
                **({} if before is None else {"diff": self._diff(self._show(p), before, content)}),
                **({"notes": notes} if notes else {}), **self._checked(p)}

    # --- whole subprograms ----------------------------------------------------
    def _current(self, p: Path, path: str, outlined: bool = False) -> tuple[str, str, bool] | dict:
        """(text with LF, encoding, CRLF?) of a file read before, or why it cannot be changed."""
        data = p.read_bytes()
        err = self._check_seen(p, path, data, outlined)
        if err:
            return err
        raw, encoding = _decode(data)
        return raw.replace("\r\n", "\n"), encoding, "\r\n" in raw

    def _store(self, p: Path, text: str, encoding: str, crlf: bool, before: str | None = None) -> dict | list[str]:
        """Writes `text` (with LF) as the file, in its encoding and line endings, and returns
        the notes of the warnings of the rules on the lines it adds. An error instead when the
        text cannot be encoded, when it breaks the syntax of a file that had none (`before`: its
        text; None for a new file, which is written anyway), or when a rule of level error
        finds something in the lines it adds (in a new file too)."""
        if self.syntax is not None and before is not None:
            errors = self.syntax(p, text)
            if errors and not self.syntax(p, before):
                return _broken_syntax(self._show(p), text, errors)
        found = self.rules(p, text, before) if self.rules is not None else []
        refused = [f for f in found if f["level"] == "error"]
        if refused:
            return _broken_rule(self._show(p), refused)
        try:
            out = (text.replace("\n", "\r\n") if crlf else text).encode(encoding)
        except UnicodeEncodeError as e:
            return {"error": f"the file is {encoding} and the new text has characters it cannot hold: {e}"}
        p.parent.mkdir(parents=True, exist_ok=True)
        self._write(p, out)
        return [f"line {f['line']}: {f['message']}" for f in found]

    def _checked(self, p: Path) -> dict:
        """{"check": what check_code says of a file just changed}, when BuildTools gave a hook."""
        found = self.after_change(p) if self.after_change is not None else None
        return {"check": found} if found else {}

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
        """Replaces a whole subprogram of a file read before (outline or read_subprogram are
        enough). `new_text` is the complete new
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
            current = self._current(p, path, outlined=True)
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
            stored = self._store(p, new, encoding, crlf, before=text)
            if isinstance(stored, dict):
                return stored
            notes = notes + stored
        return {"file": self._show(p), "replaced": f"{target.kind} of {target.qualified}, lines {target.start}-"
                                                  f"{target.end}",
                "diff": self._diff(self._show(p), text, new), **({"notes": notes} if notes else {}),
                **self._checked(p)}

    def add_subprogram(self, path: str, new_text: str, after: str = "", before: str = "") -> dict:
        """Adds new subprograms to a file read before (outline is enough); `new_text` holds them
        complete. They go
        right after the subprogram named `after`, or right before the one named `before` (with
        its comment: in Ada a subprogram of a body must come before the one that calls it), or
        else at the end of the declarations of the package (before `private` in a spec, before
        the `begin` or the `end` of a package body; at the end of a Python file). Returns the
        diff."""
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
            current = self._current(p, path, outlined=True)
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
            if after or before:
                anchor = after or before
                found = [s for s in spans if same(s.name, anchor.strip().split(".")[-1])
                         and _qualified_match(s, anchor, same)]
                bodies = [s for s in found if s.kind == "body"] or found
                if not bodies:
                    return _missing(path, anchor.strip().split(".")[-1], "", [], spans)
                target = bodies[-1] if after else bodies[0]
                indent = _indent(lines[target.start - 1])
                if after:
                    where = target.end + 1
                else:                                     # before it and before its comment
                    where, mark = target.start, comment_mark(p.suffix)
                    while where > 1 and lines[where - 2].strip().startswith(mark):
                        where -= 1
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
            stored = self._store(p, new, encoding, crlf, before=text)
            if isinstance(stored, dict):
                return stored
            notes = notes + stored
        return {"file": self._show(p), "added": [a.name for a in added],
                "diff": self._diff(self._show(p), text, new), **({"notes": notes} if notes else {}),
                **self._checked(p)}

def _broken_syntax(path: str, text: str, errors: list[dict]) -> dict:
    """The answer to a change that breaks the syntax of a file: not made, with the errors and
    the lines around the first one, as the file would have been."""
    line = errors[0].get("line")
    lines = text.split("\n")
    around = "\n".join(f"{'>' if i == line else ' '}{i:4d} | {lines[i - 1]}"
                       for i in range(max(1, line - 3), min(len(lines), line + 3) + 1)) if line else ""
    return {"error": f"the change was not made: it breaks the syntax of {path}, which had none. Correct the new "
                     "text and try again", "syntax_errors": errors[:5], **({"around": around} if around else {})}


def _broken_rule(path: str, found: list[dict]) -> dict:
    """The answer to a change that a rule of level error refuses: not made, with what the rules
    found and where."""
    first = found[0]
    return {"error": f"the change was not made: line {first['line']} of the new text of {path} breaks a rule of "
                     f"the project: {first['message']}", "rules": found[:5]}


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

register_methods(CodeTools, WRITE_TOOLS, writes=WRITE_TOOLS)
