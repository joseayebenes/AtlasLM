import json
from dataclasses import replace
from pathlib import Path

import pytest

from atlas.docs import DocsOptions, process_document
from atlas.index import Index, build_records, chunk_text
from atlas.pipeline import FatalError


@pytest.fixture
def docs(docs_samples, tmp_path, fake_llm):
    """structure.json of the sample manual and specification, analysed with the fake API."""
    pytest.importorskip("markitdown")
    return {f: process_document(docs_samples / f, tmp_path / "docs" / f, DocsOptions(), llm=fake_llm[0])
            for f in ("hmi_manual.md", "spec_with_bookmarks.pdf")}


def _add(idx, data):
    return idx.add_document(data, doc=f"test:{data['source']}", name=data["source"], out_dir=data["out_dir"])


def test_chunks_and_records(docs):
    text = " ".join(f"Sentence number {i} of the body." for i in range(200))
    chunks = chunk_text(text, size=300, overlap=60)
    assert all(len(c) <= 300 for c in chunks) and len(chunks) > 10
    assert chunks[1].startswith("Sentence number")          # the overlap starts at a sentence
    records = build_records(docs["hmi_manual.md"])
    assert {r["kind"] for r in records} == {"overview", "section_summary", "chunk", "requirement"}
    chunk = next(r for r in records if r["kind"] == "chunk" and r["section_title"] == "2 Alarms")
    assert chunk["embed_text"].startswith("HMI Manual > 2 Alarms\nSection about")   # with its section summary
    reqs = build_records(docs["hmi_manual.md"], kinds=["requirement"])
    assert [r["ref"] for r in reqs] == ["SRS-0101", "SRS-0102", "SRS-0103"]


def test_search_answer_and_cache(docs, tmp_path, fake_llm, fake_embeddings, fake_stats):
    idx = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0])
    for data in docs.values():
        assert _add(idx, data)["missing"] == 0
    hits = idx.search("SRS-0102", k=3, document="hmi_manual.md")
    assert hits[0]["ref"] == "SRS-0102" and hits[0]["text"].endswith("raise an alarm when a track is lost.")
    hits = idx.search("keep logs for 30 days", k=3)
    assert hits[0]["doc_name"] == "hmi_manual.md"
    assert {h["kind"] for h in idx.search("track event", k=10, kinds=["requirement"])} == {"requirement"}
    assert len(idx.search("track event", rewrite=True)) == 8

    res = idx.answer("When does the HMI raise an alarm?", document="HMI")
    assert "[1]" in res["answer"] and res["sources"][0]["doc"] == "hmi_manual.md"
    assert Path(res["sources"][0]["file"]).exists()

    # indexing the same document again embeds nothing
    before = fake_stats()["emb_texts"]
    _add(idx, docs["hmi_manual.md"])
    assert fake_stats()["emb_texts"] == before
    idx.remove_document("test:spec_with_bookmarks.pdf")
    assert [d["name"] for d in idx.documents()] == ["hmi_manual.md"]


def test_failed_batches_are_retried_and_connection_errors_are_fatal(docs, tmp_path, fake, fake_embeddings,
                                                                     fake_stats):
    _, control = fake
    idx = Index(tmp_path / "atlas.db", replace(fake_embeddings, batch=2))
    control.write_text(json.dumps({"emb_fail_if": ["2 Alarms"]}))
    first = _add(idx, docs["hmi_manual.md"])
    assert first["missing"] > 0 and first["failed_batches"] and first["records"] > 0
    # the rest is searchable already; next time only what is missing is embedded
    assert idx.search("keep logs for 30 days", k=1)[0]["ref"] in ("", "SRS-0103")
    control.write_text("{}")
    before = fake_stats()["emb_texts"]
    second = _add(idx, docs["hmi_manual.md"])
    assert second["missing"] == 0 and fake_stats()["emb_texts"] - before == first["missing"]

    control.write_text(json.dumps({"down": True}))
    with pytest.raises(FatalError, match="embeddings"):
        _add(idx, docs["spec_with_bookmarks.pdf"])


def test_vectors_of_another_embeddings_model_are_not_mixed(docs, tmp_path, fake_embeddings):
    db = tmp_path / "atlas.db"
    _add(Index(db, fake_embeddings), docs["hmi_manual.md"])
    other = Index(db, replace(fake_embeddings, model="nomic-embed-text"))
    assert other.store.dense(other.embedder.queries(["alarm"])[0], "nomic-embed-text", 5, None) == []
    assert other.search("alarm", k=1)            # the lexical search still finds it
    _add(other, docs["hmi_manual.md"])
    assert other.store.dense(other.embedder.queries(["alarm"])[0], "nomic-embed-text", 5, None)


def test_a_long_lived_index_sees_what_another_connection_indexes(docs, tmp_path, fake_embeddings):
    server = Index(tmp_path / "atlas.db", fake_embeddings)        # atlas serve, for instance
    runner = Index(tmp_path / "atlas.db", fake_embeddings)        # and an atlas run meanwhile
    _add(runner, docs["hmi_manual.md"])
    qvec = server.embedder.queries(["track"])[0]
    before = server.store.dense(qvec, server.embedder.model, 10_000, None)
    _add(runner, docs["spec_with_bookmarks.pdf"])
    after = server.store.dense(qvec, server.embedder.model, 10_000, None)
    total = sum(d["records"] for d in runner.documents())
    assert len(before) < len(after) == total
    runner.remove_document("test:spec_with_bookmarks.pdf")
    assert len(server.store.dense(qvec, server.embedder.model, 10_000, None)) == len(before)
