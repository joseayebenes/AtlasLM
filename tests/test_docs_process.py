import json
import re

import pytest

from atlas.docs import DocsOptions, preview, process_document
from atlas.pipeline import FatalError


def test_regex_analysis_without_a_model(docs_samples, tmp_path):
    pytest.importorskip("markitdown")
    out = tmp_path / "out" / "spec"
    data = process_document(docs_samples / "spec_with_bookmarks.pdf", out, DocsOptions(analysis="regex"))
    import pymupdf
    in_pdf = {x for page in pymupdf.open(docs_samples / "spec_with_bookmarks.pdf")
              for x in re.findall(r"SRS-\d{4}", page.get_text())}
    assert {r["id"] for r in data["requirements"]} == in_pdf and len(in_pdf) > 100
    complete = re.compile(r"The TMF shall process track event \d of group [\d.]+ within \d+ ms after "
                          r"reception \(see SSS-\d{4}\)\.")
    assert all(r["status"] == "literal" and complete.fullmatch(r["text"]) for r in data["requirements"])
    assert data["errors"] == 0 and data["model"] == "" and data["analysis_mode"] == "regex"
    assert data["structure_info"]["source"].startswith("PDF bookmarks")
    for name in ("index.md", "requirements.md", "glossary.md", "entities.md", "open_issues.md"):
        assert (out / name).exists()
    assert json.loads((out / "structure.json").read_text())["sections"][1]["body"]
    assert len(list((out / "sections").glob("*.md"))) == len(data["sections"])
    assert not (tmp_path / "out" / "spec.tmp").exists()


def test_none_analysis_and_preview(docs_samples, tmp_path):
    md = docs_samples / "hmi_manual.md"
    data = process_document(md, tmp_path / "hmi", DocsOptions(analysis="none"))
    assert data["requirements"] == [] and len(data["sections"]) == 4
    p = preview(md)
    assert p["title"] == "HMI Manual" and [s["path"][-1] for s in p["sections"]][1] == "1 General"
    assert "accepted as a heading" in p["headings"]
    with pytest.raises(ValueError, match="needs the model"):
        process_document(md, tmp_path / "x", DocsOptions(analysis="llm"))


def test_llm_analysis_cache_and_retry(docs_samples, tmp_path, fake_llm, fake_chats):
    service, control = fake_llm
    md = tmp_path / "hmi_manual.md"
    md.write_text((docs_samples / "hmi_manual.md").read_text())
    out, opts = tmp_path / "docs" / "hmi", DocsOptions(lang="Spanish")

    # the model answers in prose: every section fails, the output is written anyway
    control.write_text(json.dumps({"prose": True}))
    data = process_document(md, out, opts, llm=service)
    assert data["errors"] == 4 and all(s["analysis_error"] for s in data["sections"])
    assert "Analysis pending" in next((out / "sections").glob("002-*.md")).read_text()
    assert list((out / "_llm_failures").glob("*_attempt2.txt"))

    # running again retries them; the ones that work go to the cache
    control.write_text("{}")
    data = process_document(md, out, opts, llm=service)
    assert data["errors"] == 0 and data["overview"]["summary"] == "Overall."
    assert [r["id"] for r in data["requirements"]] == ["SRS-0101", "SRS-0102", "SRS-0103"]
    assert data["model"] == "fake" and not (out / "_llm_failures").exists()

    # nothing changed: no request at all
    before = fake_chats()
    process_document(md, out, opts, llm=service)
    assert fake_chats() == before

    # one section changed: that section and the overview
    md.write_text(md.read_text().replace("30 days", "60 days"))
    data = process_document(md, out, opts, llm=service)
    assert fake_chats() - before == 2
    assert "60 days" in data["requirements"][2]["text"]


def test_long_sections_are_split_when_the_answer_is_cut(docs_samples, tmp_path, fake_llm):
    pytest.importorskip("markitdown")
    service, control = fake_llm
    pdf = docs_samples / "spec_with_bookmarks.pdf"
    whole = process_document(pdf, tmp_path / "a" / "spec", DocsOptions(), llm=service)
    # sections of ~2000 characters are cut; their halves are not
    control.write_text(json.dumps({"cut_sections_over": 1600}))
    split = process_document(pdf, tmp_path / "b" / "spec", DocsOptions(), llm=service)
    assert split["errors"] == 0
    assert [r["id"] for r in split["requirements"]] == [r["id"] for r in whole["requirements"]]
    assert len(split["requirements"]) > 100


def test_a_connection_error_is_fatal_and_keeps_the_previous_output(docs_samples, tmp_path, fake_llm):
    service, control = fake_llm
    md, out = docs_samples / "hmi_manual.md", tmp_path / "hmi"
    process_document(md, out, DocsOptions(), llm=service, cache_dir=tmp_path / "cache")
    control.write_text(json.dumps({"down": True}))
    with pytest.raises(FatalError, match="credentials"):
        process_document(md, out, DocsOptions(lang="French"), llm=service, cache_dir=tmp_path / "cache")
    assert (out / "index.md").exists() and not (tmp_path / "hmi.tmp").exists()


def test_ctrl_c_does_not_go_on_with_the_pending_sections(docs_samples, tmp_path, fake_llm, monkeypatch):
    import time
    from atlas.docs import analysis
    calls = []

    def interrupted(*args, **kwargs):          # Ctrl+C during the first section
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        time.sleep(0.3)                         # the next one is a request in progress
        return analysis.normalize({})

    monkeypatch.setattr(analysis, "analyze_section", interrupted)
    with pytest.raises(KeyboardInterrupt):
        process_document(docs_samples / "hmi_manual.md", tmp_path / "hmi", DocsOptions(), llm=fake_llm[0], workers=1)
    # the one in progress finishes; the other two of the 4 sections are never started
    assert len(calls) <= 2 and not (tmp_path / "hmi.tmp").exists()
