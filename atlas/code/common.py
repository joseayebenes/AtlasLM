"""What every code analyzer shares: the requirement tags in comments and the place of a call."""
from __future__ import annotations

import re

__all__ = ["REQ_ID_SHAPE", "call_at", "extract_requirements", "requirement_pattern"]

# An ID has at least one separator (-, _, .) or a digit: loose words are not IDs
REQ_ID_SHAPE = re.compile(r"^(?=.*[-_.\d])[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*$")


def requirement_pattern(config: dict) -> re.Pattern:
    """The traceability tag of [sources.code]: `req_regex` if given (group 1 = the list of
    IDs), otherwise built from `req_tag` ("Req:" by default, case-insensitive)."""
    if config.get("req_regex"):
        return re.compile(config["req_regex"])
    tag = (config.get("req_tag") or "Req:").strip()
    if tag.endswith(":"):                      # "Req:" also matches "REQ :"
        return re.compile(rf"(?i)(?<!\w){re.escape(tag[:-1].strip())}\s*:\s*([^\n]+)")
    return re.compile(rf"(?i)(?<!\w){re.escape(tag)}\s*([^\n]+)")


def extract_requirements(text: str, pattern: re.Pattern) -> list[str]:
    """Requirement IDs. After the tag, tokens that look like an ID are read until one that
    does not ("Req: A-B, C-D deletes messages" -> A-B, C-D)."""
    ids = set()
    for m in pattern.finditer(text):
        rest = m.group(1) if m.groups() else m.group(0)
        for tok in re.split(r"[,\s;]+", rest.strip()):
            tok = tok.strip(".:;,()[]")
            if not tok:
                continue
            if not REQ_ID_SHAPE.match(tok):
                break
            ids.add(tok)
    return sorted(ids)


def call_at(name: str, line: int, before: bytes) -> dict:
    """A call of a CodeUnit with its place: `line` from 1, and the column from 1 in
    characters, from `before`, the bytes of the line in front of the name (parsers count
    columns in bytes)."""
    return {"name": name, "line": line, "column": len(before.decode("utf-8", errors="replace")) + 1}
