"""What every code analyzer shares: the requirement tags in comments."""
from __future__ import annotations

import re

__all__ = ["REQ_ID_SHAPE", "extract_requirements", "requirement_ids", "requirement_pattern"]

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


def requirement_ids(config: dict) -> re.Pattern | None:
    """The expression of an ID inside a tag: `id_regex` of the analyzer configuration,
    which the code.parse step takes from [sources.doors.convention] (id_regex, or id_pattern
    without its anchors). None without it."""
    return re.compile(config["id_regex"]) if config.get("id_regex") else None


def extract_requirements(text: str, pattern: re.Pattern, ids: re.Pattern | None = None) -> list[str]:
    """Requirement IDs of the tags of a text. With `ids` (the expression of an ID), every
    ID in the rest of the line after the tag ("Req: SRS-0004 and SRS-0005", "Req: see
    SRS-0004", "Req: SRS-0004 / SRS-0005"). Without it, tokens that look like an ID are read
    until one that does not ("Req: A-B, C-D deletes messages" -> A-B, C-D)."""
    found = set()
    for m in pattern.finditer(text):
        rest = m.group(1) if m.groups() else m.group(0)
        if ids is not None:
            found.update(i.group(0) for i in ids.finditer(rest))
            continue
        for tok in re.split(r"[,\s;]+", rest.strip()):
            tok = tok.strip(".:;,()[]")
            if not tok:
                continue
            if not REQ_ID_SHAPE.match(tok):
                break
            found.add(tok)
    return sorted(found)
