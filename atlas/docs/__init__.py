"""Documents: text extractors as plugins. Structure, analysis and Markdown output stay
in legacy/pdf_struct.py until it is migrated (phase 1 of the plan)."""
from __future__ import annotations

from pathlib import Path

from atlas.plugins import DocumentExtractor, register


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
