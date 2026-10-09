"""Documentos: extractores de texto como plugins. La estructura, el análisis y los
Markdown siguen en legacy/pdf_struct.py hasta su migración (fase 1 del plan)."""
from __future__ import annotations

from pathlib import Path

from atlas.plugins import DocumentExtractor, register


class MarkitdownExtractor(DocumentExtractor):
    name = "markitdown"
    extensions = (".pdf", ".docx", ".pptx", ".xlsx", ".html")

    def extract(self, path: Path) -> str:
        from markitdown import MarkItDown   # dependencia opcional [docs]
        return MarkItDown().convert(str(path)).text_content


class TextExtractor(DocumentExtractor):
    name = "text"
    extensions = (".md", ".markdown", ".txt")

    def extract(self, path: Path) -> str:
        return Path(path).read_text(encoding="utf-8", errors="replace")


register("extractor", "markitdown", MarkitdownExtractor)
register("extractor", "text", TextExtractor)
