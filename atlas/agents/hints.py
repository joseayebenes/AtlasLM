"""Drafts of hints from the calls of atlas develop: the errors of the compiler that came without
a hint, each with how the agent got past it, for a person to write the hint.

    drafts = draft_hints("workspace/reports/develop-server.calls.jsonl")
    write_hints_draft("workspace/reports/develop-server.calls.jsonl")   # -> reports/hints-server.toml
    atlas hints tasks/server.toml atlas.toml                            # the same, from the command line

The errors come from the answers of the tools in the calls log of each run: the `check` of a
change (check_on_edit), the `syntax_errors` of a change refused, and `check_code`. They are
grouped by their shape (the message without its names and numbers: the same error in other
places). For each one, the first later write in the same run to the same file whose `check` no
longer has an error of that shape is how it was fixed: its diff goes with the draft.

The draft is a TOML file like those of [sources.code] hints, one [[hint]] per shape, the most
frequent first: a `match` derived from the message ("X" -> a named group, numbers -> \\d+, the
rest as it is), `hint = ""`, and the message, its details and the diff of the fix as comments.
It is never used as it is: a person reads it, writes the hints worth having and copies them to
a file of [sources.code] hints. In the short run of 2026-10-15, the two hints of the most
frequent errors without one were a commit by hand after reading the log.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from atlas.code.tools import WRITE_TOOLS

__all__ = ["draft_hints", "hint_match", "shape", "write_hints_draft"]

MAX_DIFF_LINES = 30          # lines of the diff of a fix in the draft
QUOTED_OR_NUMBER = re.compile(r'"[^"]*"|\d+')


def shape(message: str) -> str:
    """A message of the compiler without its names and numbers: the same error in other places."""
    return re.sub(r"\d+", "N", re.sub(r'"[^"]*"', '"X"', message)).strip()


def hint_match(message: str) -> str:
    """A regular expression for a message and the others of its shape: each quoted name a named
    group ("(?P<name>[^"]+)", then name2...), each number \\d+, the rest escaped."""
    out, last, names = [], 0, 0
    for m in QUOTED_OR_NUMBER.finditer(message):
        out.append(re.escape(message[last:m.start()]))
        if m.group(0).startswith('"'):
            names += 1
            out.append(f'"(?P<name{names if names > 1 else ""}>[^"]+)"')
        else:
            out.append(r"\d+")
        last = m.end()
    out.append(re.escape(message[last:]))
    return "".join(out).replace("\\ ", " ").strip()


def _errors(call: dict) -> list[tuple[dict, bool]]:
    """The errors of the compiler in the answer of a tool call, with a hint or without, each
    with whether it is a syntax error (of a change that was refused for it)."""
    res = call.get("result") if isinstance(call.get("result"), dict) else {}
    name = call.get("name")
    if name in WRITE_TOOLS:
        check = res.get("check") if isinstance(res.get("check"), dict) else {}
        return [*((e, True) for e in res.get("syntax_errors", [])), *((e, False) for e in check.get("errors", []))]
    if name == "check_code":
        return [(e, False) for e in res.get("errors", [])]
    return []


def _file(call: dict) -> str:
    res = call.get("result") if isinstance(call.get("result"), dict) else {}
    return str(res.get("file") or (call.get("args") or {}).get("path") or "")


def _fix(calls: list[dict], i: int, file: str, key: str, syntax: bool = False) -> dict | None:
    """The first write after calls[i] to `file` that was made and whose check has no error of
    the shape `key` (for a syntax error, any write made: it passed the syntax check): {diff,
    call, file}; None when the run has none."""
    same = lambda c: _file(c) == file or Path(_file(c)).name == Path(file).name
    for j in range(i + 1, len(calls)):
        c = calls[j]
        res = c.get("result") if isinstance(c.get("result"), dict) else {}
        if c.get("name") not in WRITE_TOOLS or not same(c) or "error" in res:
            continue
        check = res.get("check") if isinstance(res.get("check"), dict) else None
        if check is None and not syntax:             # nothing says whether the error is gone
            continue
        if check and any(shape(e.get("message", "")) == key for e in check.get("errors", [])):
            continue
        diff = res.get("diff") or (c.get("args") or {}).get("content") or (c.get("args") or {}).get("new_text") or ""
        return {"diff": str(diff), "call": c.get("name"), "file": _file(c)}
    return None


def draft_hints(calls_path: str | Path) -> list[dict]:
    """The errors without a hint of a calls log of atlas develop, by shape, the most frequent
    first: {shape, match, message, details, count, files, runs, fix} where fix is the first
    write after it in the same run that got rid of it ({diff, call, file, run}) or None."""
    found: dict[str, dict] = {}
    for line in Path(calls_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            continue
        calls = run.get("calls") or []
        for i, call in enumerate(calls):
            for e, syntax in _errors(call):
                message = str(e.get("message", "")).strip()
                if e.get("hint") or not message:
                    continue
                key = shape(message)
                file = _file(call) or str(e.get("file") or "")
                d = found.setdefault(key, {"shape": key, "match": hint_match(message), "message": message,
                                           "details": list(e.get("details", [])), "count": 0, "files": [],
                                           "runs": [], "fix": None})
                d["count"] += 1
                if file and Path(file).name not in [Path(f).name for f in d["files"]]:
                    d["files"].append(file)
                if run.get("n") is not None and run["n"] not in d["runs"]:
                    d["runs"].append(run["n"])
                if d["fix"] is None:
                    fix = _fix(calls, i, file, key, syntax)
                    if fix:
                        d["fix"] = {**fix, "run": run.get("n")}
                        d["message"], d["details"] = message, list(e.get("details", []))  # the one it fixes
    return sorted(found.values(), key=lambda d: (-d["count"], d["shape"]))


def _toml_string(s: str) -> str:
    """A TOML string: literal ('...') when it can be, else basic (JSON's escapes are TOML's)."""
    return f"'{s}'" if "'" not in s and "\n" not in s else json.dumps(s, ensure_ascii=False)


def _comment(text: str, prefix: str = "#   ") -> list[str]:
    return [(prefix + l).rstrip() for l in text.splitlines()]


def draft_toml(drafts: list[dict], source: str = "") -> str:
    """The text of a draft of hints (see the module)."""
    out = [f"# Draft hints from {source or 'the calls of atlas develop'}: one [[hint]] per error that came",
           "# without a hint, the most frequent first. Write each hint (what the agent should do when it gets",
           "# that error), delete those not worth one, check the match, and copy them to a file of",
           "# [sources.code] hints. Nothing here is used as it is: an empty hint says nothing."]
    if not drafts:
        out += ["", "# Every error of the compiler came with a hint."]
    for d in drafts:
        runs = ", ".join(str(r) for r in d["runs"])
        out += ["", f"# {d['count']} time{'s' if d['count'] != 1 else ''}"
                    + (f", in {', '.join(d['files'])}" if d["files"] else "") + (f" (runs {runs})" if runs else ""),
                *_comment(d["message"], "# message: ")]
        out += [*(l for detail in d["details"] for l in _comment(detail, "# detail: "))]
        if d["fix"]:
            lines = d["fix"]["diff"].splitlines()
            out += [f"# how the agent got past it (run {d['fix']['run']}, {d['fix']['call']} of {d['fix']['file']}):",
                    *_comment("\n".join(lines[:MAX_DIFF_LINES]))]
            if len(lines) > MAX_DIFF_LINES:
                out.append(f"#   … ({len(lines) - MAX_DIFF_LINES} more lines)")
        else:
            out.append("# no write in the same run got rid of it")
        out += ["[[hint]]", f"match = {_toml_string(d['match'])}", 'hint = ""']
    return "\n".join(out) + "\n"


def write_hints_draft(calls_path: str | Path, path: str | Path | None = None) -> tuple[Path, list[dict]]:
    """Writes the draft of the hints of a calls log (see the module) and returns (its path, the
    drafts). By default next to it: reports/develop-<plan>.calls.jsonl -> reports/hints-<plan>.toml."""
    calls_path = Path(calls_path)
    if path is None:
        plan = calls_path.name.removesuffix(".calls.jsonl").removeprefix("develop-")
        path = calls_path.with_name(f"hints-{plan}.toml")
    drafts = draft_hints(calls_path)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(draft_toml(drafts, calls_path.name), encoding="utf-8")
    return path, drafts
