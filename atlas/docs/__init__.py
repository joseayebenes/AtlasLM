"""Documents: text extraction, structure, analysis and Markdown output (legacy/pdf_struct.py).

    structure.py   sections without a model: bookmarks, headings or fixed size
    analysis.py    section analysis: llm | regex | none; verification and consolidation
    writer.py      Markdown and structure.json
    process.py     a whole document: DocsOptions, process_document, preview, pending_requests

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
           "pending_requests", "preview", "process_document", "read_document", "read_text"]


class PymupdfExtractor(DocumentExtractor):
    """PDF with pymupdf (MuPDF): some 30 times faster than pdfminer (markitdown), and the
    text keeps the order in which it was written, so a heading number stays next to its
    title. Each text block is a paragraph: blocks are separated by a blank line, as the
    sections and the chunks of the index expect."""
    name = "pymupdf"
    extensions = (".pdf",)
    priority = 10           # before markitdown; [plugins] disabled = ["extractor:pymupdf"] goes back to it
    license = "AGPL-3.0 or commercial (pymupdf, Artifex)"     # see docs/seguridad.md, "Dependencias"

    def extract(self, path: Path) -> str:
        return self.read(path)[0]

    def read(self, path: Path) -> tuple[str, dict]:
        """With `tables` in the config ([[sources.docs]] tables = true), each table that
        page.find_tables() finds replaces the blocks inside it with its rows in Markdown."""
        try:
            import pymupdf                  # optional dependency [docs]
        except ImportError:
            import fitz as pymupdf          # pymupdf before 1.24.3
        texts, without_text, tables = [], 0, 0
        with pymupdf.open(str(path)) as doc:
            for page in doc:
                raw = page.get_text("blocks")
                blocks = [b[4].strip() for b in raw if b[6] == 0 and b[4].strip()]
                if not blocks and page.get_images():
                    without_text += 1       # an image and no text: a scanned page
                if blocks and self.config.get("tables"):
                    blocks, found = _with_tables(page, pymupdf, raw)
                    tables += found
                texts.append("\n\n".join(blocks))
            metadata = {k: v for k, v in (doc.metadata or {}).items() if isinstance(v, str) and v.strip()}
        info = {"pages": len(texts), "pages_without_text": without_text, "metadata": metadata}
        if self.config.get("tables"):
            info["tables"] = tables
        return "\f".join(texts), info


def _cell(text) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


def _with_tables(page, pymupdf, raw_blocks: list) -> tuple[list[str], int]:
    """The text blocks of a page in order, with the blocks that fall inside
    each table replaced by its rows in Markdown ("| c1 | c2 |", the header, then
    "|---|---|", then one line per row), placed where its first block was; and how many
    tables. A block belongs to a table when most of its area is inside it."""
    getattr(pymupdf, "no_recommend_layout", lambda: None)()     # it prints to stdout: the MCP channel of atlas serve
    try:
        found = page.find_tables().tables
    except Exception:               # a page find_tables cannot read: its text as it is
        found = []
    tables = []
    for t in found:
        rows = [list(r) for r in t.extract() if any(str(c or "").strip() for c in r)]
        rect = pymupdf.Rect(t.bbox)
        header = getattr(t, "header", None)
        if header is not None and getattr(header, "external", False) and header.names:
            rows.insert(0, list(header.names))
            rect |= pymupdf.Rect(header.bbox)
        if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
            tables.append((rect, rows))
    out, placed = [], set()
    for b in raw_blocks:
        if b[6] != 0 or not b[4].strip():
            continue
        r = pymupdf.Rect(b[:4])
        inside = next((k for k, (rect, _) in enumerate(tables)
                       if r.get_area() and (r & rect).get_area() > 0.5 * r.get_area()), None)
        if inside is None:
            out.append(b[4].strip())
        elif inside not in placed:
            placed.add(inside)
            rows = tables[inside][1]
            width = max(len(row) for row in rows)
            lines = ["| " + " | ".join(_cell(c) for c in row + [""] * (width - len(row))) + " |" for row in rows]
            lines.insert(1, "|" + "---|" * width)
            out.append("\n".join(lines))
    return out, len(placed)


class MarkitdownExtractor(DocumentExtractor):
    name = "markitdown"
    extensions = (".pdf", ".docx", ".pptx", ".xlsx", ".html")
    license = "MIT (markitdown, pdfminer.six)"

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


def read_document(path: str | Path, disabled: Iterable[str] = (), config: dict | None = None) -> tuple[str, dict]:
    """(text, what the extractor knows about the file: pages, pages_without_text, metadata
    and, with config {"tables": True}, how many tables became Markdown)."""
    return extractor_for(path, disabled, config).read(Path(path))


from atlas.docs.process import DocsOptions, pending_requests, preview, process_document  # noqa: E402  (needs read_document)
