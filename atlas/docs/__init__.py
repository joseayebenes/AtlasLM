"""Documents: text extraction, structure, analysis and Markdown output (legacy/pdf_struct.py).

Text extractors are plugins chosen by file extension: "markitdown" (PDF, Office, HTML) and
"text" (Markdown and plain text). The structure (sections) comes from `atlas.docs.structure`.

    from atlas.docs import read_text
    from atlas.docs.structure import clean_headers, prepare_sections
    text, _ = clean_headers(read_text("spec.pdf"))
    sections = prepare_sections(text, pdf=Path("spec.pdf"))
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from atlas import plugins
from atlas.plugins import DocumentExtractor, register

__all__ = ["MarkitdownExtractor", "TextExtractor", "extractor_for", "read_text"]


class MarkitdownExtractor(DocumentExtractor):
    name = "markitdown"
    extensions = (".pdf", ".docx", ".pptx", ".xlsx", ".html")

    def extract(self, path: Path) -> str:
        from markitdown import MarkItDown   # optional dependency [docs]
        return MarkItDown().convert(str(path)).text_content


class TextExtractor(DocumentExtractor):
    name = "text"
    extensions = (".md", ".markdown", ".txt")

    def extract(self, path: Path) -> str:
        return Path(path).read_text(encoding="utf-8", errors="replace")


register("extractor", "markitdown", MarkitdownExtractor)
register("extractor", "text", TextExtractor)


def extractor_for(path: str | Path, disabled: Iterable[str] = (), config: dict | None = None) -> DocumentExtractor:
    """The first enabled extractor (by name) that handles the extension of `path`."""
    suffix = Path(path).suffix.lower()
    disabled = set(disabled)
    for name in plugins.available("extractor"):
        if f"extractor:{name}" in disabled:
            continue
        cls = plugins.get("extractor", name)
        if suffix in cls.extensions:
            return cls(config or {})
    raise LookupError(f"no enabled extractor for '{suffix}' files ({path})")


def read_text(path: str | Path, disabled: Iterable[str] = ()) -> str:
    """Text of a document, with \\f between pages if it has them."""
    return extractor_for(path, disabled).extract(Path(path))
