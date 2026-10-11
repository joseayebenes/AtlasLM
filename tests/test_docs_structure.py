import pytest

from atlas.docs import read_text
from atlas.docs.structure import (clean_headers, detect_sections, diagnose_headings, document_title,
                                  prepare_sections, split_text)


@pytest.fixture(scope="module")
def pdf_text():
    pytest.importorskip("pymupdf")

    def _read(path, disabled=()):
        return clean_headers(read_text(path, disabled))
    return _read


def test_sections_from_the_pdf_bookmarks(docs_samples, pdf_text):
    pytest.importorskip("pymupdf")
    pdf = docs_samples / "spec_with_bookmarks.pdf"
    text, removed = pdf_text(pdf)
    assert removed > 20 and "Company Confidential" not in text and "Page 3 of 99" not in text
    info = {}
    sections = prepare_sections(text, pdf=pdf, info=info)
    assert info["source"] == "PDF bookmarks (12 of 12 found in the text)"
    assert [s["id"] for s in sections] == [f"{i:03d}" for i in range(1, 11)]
    s = sections[1]
    assert s["path"] == ["1 Chapter 1 Functions", "1.1 Function group 1.1"] and s["page_start"] == 2
    assert "Req: SRS-0001 The TMF shall" in s["body"]
    assert document_title(text, pdf) == "Track Management Software Requirements Specification"


def test_sections_from_numbered_headings(docs_samples, pdf_text):
    pdf = docs_samples / "spec_without_bookmarks.pdf"
    text, _ = pdf_text(pdf)
    info = {}
    sections = prepare_sections(text, pdf=pdf, info=info)
    # 3 chapters, 9 groups and 27 subsections (the table of contents is left out), and the preamble
    assert info["source"] == "text headings (39 detected)"
    assert len(sections) == 28 and sections[0]["title"] == "Preamble"
    paths = [" > ".join(s["path"]) for s in sections]
    assert "1 Chapter 1 Functions > 1.1 Function group 1.1 > 1.1.1 Track handling 1.1.1" in paths
    # every requirement in its subsection, and each one whole (its two lines in one paragraph)
    s112 = next(s for s in sections if s["title"] == "1.1.2 Track handling 1.1.2")
    assert s112["body"].startswith("Req: SRS-0007 The TMF shall process track event 0 of\ngroup 1.1.2 within")
    assert "\n\nReq: SRS-0008" in s112["body"]
    # the table of contents is not taken for headings
    assert any(r == "table of contents entry" for r in diagnose_headings(text))
    # forcing bookmarks on a PDF without them falls back to the headings and says so
    prepare_sections(text, pdf=pdf, structure="bookmarks", info=info)
    assert info["source"].startswith("the PDF has no usable bookmarks; headings used instead")


def test_sections_from_numbered_headings_split_by_pdfminer(docs_samples, pdf_text):
    """markitdown (pdfminer), when pymupdf is disabled: the numbers come apart from their
    titles and are paired again; the headings that come before their number are lost, and
    with them the second and third subsections of each group."""
    pytest.importorskip("markitdown")
    pdf = docs_samples / "spec_without_bookmarks.pdf"
    text, _ = pdf_text(pdf, ["extractor:pymupdf"])
    info = {}
    sections = prepare_sections(text, pdf=pdf, info=info)
    assert info["source"] == "text headings (21 detected)"
    paths = [" > ".join(s["path"]) for s in sections]
    assert "1 Chapter 1 Functions > 1.1 Function group 1.1 > 1.1.1 Track handling 1.1.1" in paths
    assert "1 Chapter 1 Functions > 1.1 Function group 1.1 > 1.1.2 Track handling 1.1.2" not in paths


def test_markdown_sizes_and_merging(docs_samples):
    md = docs_samples / "hmi_manual.md"
    text = read_text(md)
    sections = prepare_sections(text, pdf=md)
    assert [s["title"] for s in sections] == ["Preamble", "1 General", "2 Alarms", "3 Logging"]
    assert document_title(text, md) == "HMI Manual"
    # fixed-size blocks
    info = {}
    blocks = prepare_sections(text, max_chars=100, structure="size", info=info)
    assert info["source"] == "fixed-size blocks" and len(blocks) > 1
    assert all(len(b["body"]) <= 100 for b in blocks) and blocks[0]["title"] == "Block 1"
    with pytest.raises(ValueError, match="structure"):
        prepare_sections(text, structure="pages")


def test_short_sections_merge_inside_a_chapter_and_large_ones_split():
    text = "1 Intro\n\n" + "intro text " * 5 + "\n\n1.1 Scope\n\nshort\n\n2 Body\n\n" + ("word " * 300)
    sections = prepare_sections(text, max_chars=600, min_chars=100)
    assert sections[0]["title"] == "1 Intro" and sections[0]["includes"] == ["1.1 Scope"]
    assert [s["title"] for s in sections[1:]] == ["2 Body (part 1/3)", "2 Body (part 2/3)", "2 Body (part 3/3)"]
    assert split_text("a" * 10, 20) == ["a" * 10]


def test_pages_follow_the_page_breaks():
    text = "1 First\n\nbody one\n\f2 Second\n\nbody two\n\fmore of two"
    s = detect_sections(text)
    assert [(x["title"], x["page_start"], x["page_end"]) for x in s] == [("1 First", 1, 1), ("2 Second", 2, 3)]


def test_template_lines_at_page_edges_are_not_headers():
    pages = []
    for n in range(1, 9):
        body = [f"Req: SRS-{n}{k} The TMF shall process event {k} of" for k in range(6)]
        pages.append("\n".join(["ACME - Confidential", f"Doc 7 Issue {n}"] + body + [f"Page {n} of 8"]))
    text, removed = clean_headers("\f".join(pages))
    assert removed == 3 * 8 and "Confidential" not in text and "Page 3 of 8" not in text
    assert text.count("Req: SRS-") == 6 * 8         # the first and last ones of each page stay
