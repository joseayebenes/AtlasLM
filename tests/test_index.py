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


def test_doc_tools(docs, tmp_path, fake_llm, fake_embeddings):
    from atlas.index import DocTools
    from atlas.store import Store
    idx = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0])
    for data in docs.values():
        _add(idx, data)
    tools = DocTools(idx, Store(tmp_path))
    listed = tools.list_documents()
    assert listed["count"] == 2 and listed["documents"][0]["name"] == "hmi_manual.md"
    assert tools.search("alarm", kind="pages")["error"] == "unknown kind 'pages'"
    assert {r["kind"] for r in tools.search("track", k=50, kind="requirement")["results"]} == {"requirement"}
    assert len(tools.search("track", k=50)["results"]) == 20                       # at most 20
    section = tools.get_section("HMI", "3")                    # part of the title; its number in the document
    assert (section["section"], section["title"]) == ("004", "3 Logging") and "30 days" in section["markdown"]
    assert tools.get_section("hmi_manual.md", "003")["title"] == "2 Alarms"         # the ID of a search result
    assert tools.get_section("hmi_manual.md", "alarms")["section"] == "003"         # the title without number
    chapter = tools.get_section("spec_with_bookmarks.pdf", "2")                     # a chapter: its sections
    assert chapter == {"error": "section '2' is ambiguous",
                       "sections": [f"00{n}: 2.{n - 4} Function group 2.{n - 4}" for n in (5, 6, 7)]}
    missing = tools.get_section("manual.pdf", "003")
    assert missing == {"error": "document 'manual.pdf' is not indexed",
                       "documents": ["hmi_manual.md", "spec_with_bookmarks.pdf"]}
    assert "003: 2 Alarms" in tools.get_section("hmi_manual.md", "099")["sections"]
    req = tools.get_requirement("SRS-0102")                    # not in DOORS here: only the documents
    assert "official" not in req and req["documents"][0]["cite"].startswith("hmi_manual.md › 2 Alarms")
    assert tools.get_requirement("SRS-9999") == {"error": "requirement 'SRS-9999' not found", "similar": []}


MODULE = [   # rows of Store.requirements() for a module read with the exchange adapter
    {"id": "SRS-0900", "module": "SRS_TMS", "text": "Track management", "parent": None,
     "attributes": {"kind": "heading", "heading": "Track management", "outline": "1"}},
    {"id": "SRS-0910", "module": "SRS_TMS", "text": "Deletion", "parent": "SRS-0900",
     "attributes": {"kind": "heading", "heading": "Deletion", "outline": "1.1"}},
    {"id": "SRS-0004", "module": "SRS_TMS", "text": "The TMF shall delete a lost track within 320 ms.",
     "parent": "SRS-0910", "attributes": {"kind": "requirement", "status": "Approved"}},
    {"id": "SRS-0005", "module": "SRS_TMS", "text": "Deleted tracks shall be logged.", "parent": "SRS-0004",
     "attributes": {"kind": "requirement"}},
    {"id": "SRS-0911", "module": "SRS_TMS", "text": "A track is lost after three missed scans.",
     "parent": "SRS-0910", "attributes": {"kind": "information"}},
    {"id": "SRS-0001", "module": "SRS_TMS", "text": "Every track has an identifier.", "parent": None,
     "attributes": {}},                                               # the database adapter: no kind
]


def test_doors_requirements_in_the_index(docs, tmp_path, fake_llm, fake_embeddings):
    from atlas.index import DocTools, requirement_records
    from atlas.store import Store
    records = requirement_records("SRS_TMS", MODULE)
    assert [(r["kind"], r.get("ref", "")) for r in records] == [("requirement", "SRS-0004"),
                                                               ("requirement", "SRS-0005"), ("chunk", ""),
                                                               ("requirement", "SRS-0001")]
    assert records[0]["path"] == "1 Track management > 1.1 Deletion" and records[0]["section_id"] == "SRS-0910"
    assert records[1]["section_title"] == "1.1 Deletion"            # under a requirement: the heading above it
    assert records[0]["embed_text"] == ("SRS_TMS > 1 Track management > 1.1 Deletion > requirement\n"
                                        "SRS-0004: The TMF shall delete a lost track within 320 ms.")
    assert records[3]["path"] == "" and records[3]["embed_text"].startswith("SRS_TMS > requirement\n")
    assert [r["kind"] for r in requirement_records("SRS_TMS", MODULE, kinds=["chunk"])] == ["chunk"]

    idx = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0])
    _add(idx, docs["hmi_manual.md"])
    assert idx.add_module("SRS_TMS", MODULE)["kinds"] == {"requirement": 3, "chunk": 1}
    hit = idx.search("SRS-0004", k=1)[0]
    assert (hit["doc"], hit["doc_name"], hit["ref"]) == ("doors:SRS_TMS", "SRS_TMS", "SRS-0004")
    tools = DocTools(idx, Store(tmp_path))
    assert tools.search("SRS-0004", k=1)["results"][0]["cite"] == "SRS_TMS › 1.1 Deletion (requirement)"
    assert [d["source"] for d in tools.list_documents()["documents"]] == ["DOORS", "document"]
    assert "DOORS module" in tools.get_section("SRS_TMS", "1")["error"]
    assert "not found" in tools.get_requirement("SRS-0004")["error"]  # its DOORS record is not a document
    assert {h["document"] for h in tools.search("track", k=20, document="SRS_TMS")["results"]} == {"SRS_TMS"}
    idx.remove_document("doors:SRS_TMS")
    assert [d["name"] for d in idx.documents()] == ["hmi_manual.md"]
