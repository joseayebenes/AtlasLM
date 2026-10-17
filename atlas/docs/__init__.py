"""Documents: text extraction, structure, analysis and Markdown output (legacy/pdf_struct.py).

    structure.py   sections without a model: bookmarks, headings or fixed size
    analysis.py    section analysis: llm | regex | none; verification and consolidation
    writer.py      Markdown and structure.json
    process.py     a whole document: DocsOptions, process_document, preview

Text extractors are plugins chosen by file extension: "pymupdf" (PDF), "markitdown" (Office,
HTML, and PDF when pymupdf is disabled) and "text" (Markdown and plain text).

    from atlas.docs import DocsOptions, process_document
    data = process_document("spec.pdf", "out/spec", DocsOptions(analysis="regex"))
    data = process_document("design.pdf", "out/design", DocsOptions(lang="Spanish"), llm=conn.llm)
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from atlas import plugins
from atlas.plugins import DocumentExtractor, register

__all__ = ["DocsOptions", "MarkitdownExtractor", "PymupdfExtractor", "TextExtractor", "extractor_for",
           "preview", "process_document", "read_document", "read_text"]


class PymupdfExtractor(DocumentExtractor):
    """PDF with pymupdf (MuPDF): some 30 times faster than pdfminer (markitdown), and the
    text keeps the order in which it was written, so a heading number stays next to its
    title. Each text block is a paragraph: blocks are separated by a blank line, as the
    sections and the chunks of the index expect."""
    name = "pymupdf"
    extensions = (".pdf",)
    priority = 10           # before markitdown; [plugins] disabled = ["extractor:pymupdf"] goes back to it

    def extract(self, path: Path) -> str:
        return self.read(path)[0]

    def read(self, path: Path) -> tuple[str, dict]:
        try:
            import pymupdf                  # optional dependency [docs]
        except ImportError:
            import fitz as pymupdf          # pymupdf before 1.24.3
        texts, without_text = [], 0
        with pymupdf.open(str(path)) as doc:
            for page in doc:
                blocks = [b[4].strip() for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
                if not blocks and page.get_images():
                    without_text += 1       # an image and no text: a scanned page
                texts.append("\n\n".join(blocks))
            metadata = {k: v for k, v in (doc.metadata or {}).items() if isinstance(v, str) and v.strip()}
        return "\f".join(texts), {"pages": len(texts), "pages_without_text": without_text, "metadata": metadata}


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


register("extractor", "pymupdf", PymupdfExtractor)
register("extractor", "markitdown", MarkitdownExtractor)
register("extractor", "text", TextExtractor)


def extractor_for(path: str | Path, disabled: Iterable[str] = (), config: dict | None = None) -> DocumentExtractor:
    """The enabled extractor that handles the extension of `path`: the one with the highest
    priority, and among equals the first by name."""
    suffix = Path(path).suffix.lower()
    disabled = set(disabled)
    found = [plugins.get("extractor", name) for name in plugins.available("extractor")
             if f"extractor:{name}" not in disabled]
    found = [cls for cls in found if suffix in cls.extensions]
    if not found:
        raise LookupError(f"no enabled extractor for '{suffix}' files ({path})")
    return max(found, key=lambda cls: cls.priority)(config or {})


def read_text(path: str | Path, disabled: Iterable[str] = ()) -> str:
    """Text of a document, with \\f between pages if it has them."""
    return extractor_for(path, disabled).extract(Path(path))


def read_document(path: str | Path, disabled: Iterable[str] = ()) -> tuple[str, dict]:
    """(text, what the extractor knows about the file: pages, pages_without_text, metadata)."""
    return extractor_for(path, disabled).read(Path(path))


from atlas.docs.process import DocsOptions, preview, process_document  # noqa: E402  (needs read_document)
