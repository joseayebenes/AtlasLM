"""Text that looks the same and is not: the dashes and spaces of Unicode.

A model writes the IDs with the hyphen that does not break a line (gpt-oss-120b writes
"SRS‑0004" with U+2011), a PDF or a word processor puts an en dash or a non-breaking space,
and a literal comparison with "SRS-0004" fails. `plain` turns them into the ASCII hyphen and
space before comparing or looking up an ID:

    plain("SRS‑0004")      -> "SRS-0004"
"""
from __future__ import annotations

__all__ = ["plain"]

DASHES = "‐‑‒–—―−﹣－"      # hyphen, non-breaking, figure, en, em, minus...
SPACES = "     　"                     # non-breaking, figure, narrow, thin...
_TABLE = {**{ord(c): "-" for c in DASHES}, **{ord(c): " " for c in SPACES}, 0x00ad: None}   # soft hyphen: out


def plain(text: str) -> str:
    """`text` with the Unicode dashes as "-", its spaces as " " and no soft hyphens."""
    return text.translate(_TABLE)
