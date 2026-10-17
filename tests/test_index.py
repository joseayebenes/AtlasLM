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


def test_the_texts_of_several_documents_go_in_full_batches(docs, tmp_path, fake, fake_embeddings, fake_stats):
    import math
    _, control = fake
    idx = Index(tmp_path / "atlas.db", fake_embeddings)              # batches of 16
    texts = [r["embed_text"] for data in docs.values() for r in build_records(data)]
    distinct = len(set(texts))
    alone = sum(math.ceil(len({r["embed_text"] for r in build_records(d)}) / 16) for d in docs.values())
    before = fake_stats()
    res = idx.embed_missing(texts, workers=3)
    grown = {k: fake_stats()[k] - before[k] for k in ("embeddings", "emb_texts")}
    assert res == {"texts": distinct, "cached": 0, "embedded": distinct, "failed_batches": []}
    assert grown == {"embeddings": math.ceil(distinct / 16), "emb_texts": distinct}
    assert grown["embeddings"] < alone                       # fewer requests than document by document
    before = fake_stats()
    for data in docs.values():                               # indexing finds everything in the cache
        assert _add(idx, data)["missing"] == 0
    assert idx.embed_missing(texts)["cached"] == distinct and fake_stats() == before

    idx2 = Index(tmp_path / "other.db", fake_embeddings)
    control.write_text(json.dumps({"emb_fail_if": ["2 Alarms"]}))
    res = idx2.embed_missing(texts, workers=2)
    assert res["failed_batches"] and 0 < res["embedded"] < distinct
    control.write_text(json.dumps({"down": True}))
    with pytest.raises(FatalError, match="embeddings"):
        idx2.embed_missing(texts)


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


def test_a_few_hits_of_each_document():
    """The elements of a code unit all repeat its name: without a limit, one unit can take
    every place of a search."""
    from atlas.index.search import diverse
    records = [{"id": i, "doc": doc} for i, doc in enumerate("aaaaabcab")]
    assert [r["id"] for r in diverse(records, 4, 2)] == [0, 1, 5, 6]
    assert [r["id"] for r in diverse(records, 8, 2)] == [0, 1, 2, 3, 4, 5, 6, 8]    # few documents: the rest
    assert [r["id"] for r in diverse(records, 3, 9)] == [0, 1, 2]


def test_search_keeps_a_few_hits_of_each_document(docs, tmp_path, fake_llm, fake_embeddings):
    idx = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0], {"results": 4, "per_document": 2})
    for data in docs.values():
        _add(idx, data)
    hits = idx.search("track alarm shall")
    assert len(hits) == 4 and [h["doc_name"] for h in hits].count("hmi_manual.md") == 2
    assert len(idx.search("track alarm shall", per_document=0)) == 4
    assert {h["doc_name"] for h in idx.search("track alarm shall", k=6, document="spec_with_bookmarks.pdf")} \
        == {"spec_with_bookmarks.pdf"}                                       # one document: no limit


def test_whole_sources_follow_the_document(docs, tmp_path, fake_llm, fake_embeddings):
    from atlas.index import requirement_records  # noqa: F401
    from atlas.index.records import reflow
    from atlas.index.search import whole_sources
    idx = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0])
    data = docs["spec_with_bookmarks.pdf"]
    idx.add_document(data, doc="test:spec", name="spec.pdf", out_dir=data["out_dir"], chunk_chars=300,
                     chunk_overlap=80)
    sources = whole_sources(idx.store, "test:spec")
    bodies = [s for s in data["sections"] if s.get("body", "").strip()]
    assert [s["section_id"] for s in sources] == [s["id"] for s in bodies]
    for source, section in zip(sources, bodies):                   # the whole text, without the overlaps
        assert " ".join(source["context"].split()) == " ".join(reflow(section["body"]).split())
    assert sources[0]["kind"] == "section" and sources[0]["page_start"] >= 1

    idx.add_module("SRS_TMS", MODULE)                              # a module: requirements in its order
    module = whole_sources(idx.store, "doors:SRS_TMS")
    assert [s["section_title"] for s in module] == ["1.1 Deletion", ""]
    assert module[0]["context"].splitlines() == ["SRS-0004: The TMF shall delete a lost track within 320 ms.",
                                                 "SRS-0005: Deleted tracks shall be logged.",
                                                 "A track is lost after three missed scans."]


def test_a_broad_question_reads_its_documents_whole(docs, tmp_path, fake_llm, fake_embeddings, fake_chats):
    idx = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0])
    for data in docs.values():
        _add(idx, data)
    idx.add_module("SRS_TMS", MODULE)

    before = fake_chats()
    res = idx.answer("When does the HMI raise an alarm?")                  # a specific question: passages
    assert res["scope"] == "passages" and res["requests"] == 2 == fake_chats() - before

    before = fake_chats()
    res = idx.answer("Summarise all the requirements of SRS_TMS")        # broad, and it names the module
    assert (res["scope"], res["documents"], res["requests"]) == ("whole", ["SRS_TMS"], 2)
    assert fake_chats() - before == 2                                    # the rewriting and one answer
    assert res["answer"] == "Answer taken from the sources [1]." and res["sources"][0]["cite"] == \
        "SRS_TMS › 1.1 Deletion (section)" and res["sources"][0]["excerpt"].startswith("SRS-0004: The TMF")

    res = idx.answer("Explain the alarms")                               # broad, no name: those of its hits
    assert res["scope"] == "whole" and "hmi_manual.md" in res["documents"]
    assert idx.answer("When does the HMI raise an alarm?", document="hmi_manual.md",
                      scope="whole")["documents"] == ["hmi_manual.md"]
    assert idx.answer("Explain the alarms", scope="passages")["scope"] == "passages"

    # too long for one request: notes of each part, combined by a last request
    small = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0], {"scope_chars": 1500, "max_parts": 50})
    before = fake_chats()
    res = small.answer("Summarise everything spec_with_bookmarks.pdf says")
    parts = res["requests"] - 2
    assert res["documents"] == ["spec_with_bookmarks.pdf"] and parts > 2 and fake_chats() - before == parts + 2
    assert res["answer"].startswith("Answer combined from the notes [1]") and len(res["sources"]) > 2
    assert [s["n"] for s in res["sources"]] == sorted(s["n"] for s in res["sources"])


def test_the_plan_of_a_broad_question_and_too_many_parts(docs, tmp_path, fake_llm, fake_embeddings, fake_chats):
    idx = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0])
    _add(idx, docs["spec_with_bookmarks.pdf"])
    idx.add_module("SRS_TMS", MODULE)
    before = fake_chats()
    dry = idx.answer("Summarise all the requirements of SRS_TMS", dry_run=True)       # no request at all
    assert fake_chats() == before and dry["documents"] == ["SRS_TMS"]
    assert {k: dry["plan"][k] for k in ("documents", "sections", "parts", "requests")} == \
        {"documents": ["SRS_TMS"], "sections": 2, "parts": 1, "requests": 2}
    res = idx.answer("Summarise all the requirements of SRS_TMS")
    assert res["requests"] == dry["plan"]["requests"] == fake_chats() - before

    small = Index(tmp_path / "atlas.db", replace(fake_embeddings), fake_llm[0], {"scope_chars": 1000, "max_parts": 2})
    plan = small.answer("Summarise spec_with_bookmarks.pdf", dry_run=True)["plan"]
    assert plan["parts"] > 2 and plan["requests"] == plan["parts"] + 2 and "minutes" not in plan
    before = fake_chats()
    res = small.answer("Summarise spec_with_bookmarks.pdf")
    assert "max_parts = 2" in res["error"] and res["plan"]["parts"] == plan["parts"] and res["chapters"]
    assert fake_chats() - before == 1                                         # only the rewriting


def test_a_section_is_read_whole(docs, tmp_path, fake_llm, fake_embeddings, fake_chats):
    from atlas.index import DocTools
    from atlas.index.search import whole_sources
    from atlas.store import Store
    idx = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0])
    for data in docs.values():
        _add(idx, data)
    idx.add_module("SRS_TMS", MODULE)
    titles = lambda doc, section: [s["section_title"] for s in whole_sources(idx.store, doc, section)]
    assert titles("test:spec_with_bookmarks.pdf", "2") == [f"2.{n} Function group 2.{n}" for n in (1, 2, 3)]
    assert titles("test:spec_with_bookmarks.pdf", "function group 2.2") == ["2.2 Function group 2.2"]
    assert titles("test:hmi_manual.md", "003") == titles("test:hmi_manual.md", "Alarms") == ["2 Alarms"]
    assert titles("doors:SRS_TMS", "1") == titles("doors:SRS_TMS", "Track management > 1.1 Deletion") == \
        titles("doors:SRS_TMS", "srs-0910") == ["1.1 Deletion"]
    assert titles("doors:SRS_TMS", "Track") == []

    before = fake_chats()
    res = idx.answer("When does the HMI raise an alarm?", document="hmi_manual.md", section="2 Alarms")
    assert (res["scope"], res["plan"]["section"], res["plan"]["sections"]) == ("whole", "2 Alarms", 1)
    assert [s["section"] for s in res["sources"]] == ["003"] and fake_chats() - before == 2
    missing = DocTools(idx, Store(tmp_path)).ask_docs("Explain it", document="hmi_manual.md", section="9")
    assert missing["error"] == "there is no section '9' in hmi_manual.md"
    assert ("hmi_manual.md", "2 Alarms") in {(c["document"], c["section"]) for c in missing["chapters"]}
    dry = idx.answer("Explain it", document="SRS_TMS", section="1.1", dry_run=True)
    assert (dry["plan"]["section"], dry["plan"]["sections"], dry["plan"]["requests"]) == ("1.1", 1, 2)


def test_notes_that_do_not_fit_are_combined_in_levels(docs, tmp_path, fake, fake_llm, fake_embeddings, fake_chats):
    _, control = fake
    idx = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0], {"scope_chars": 1000, "max_parts": 50})
    _add(idx, docs["spec_with_bookmarks.pdf"])
    question = "Summarise everything spec_with_bookmarks.pdf says"
    parts = idx.answer(question, dry_run=True)["plan"]["parts"]
    control.write_text(json.dumps({"notes_chars": 400}))       # the notes of all the parts: > 1000 characters
    before = fake_chats()
    res = idx.answer(question)
    merges = (parts + 1) // 2                                    # two notes of 400 fit in 1000
    assert res["requests"] == 1 + parts + merges + 1 == fake_chats() - before
    # the citations of the notes (the first source of each part) survive the two levels,
    # renumbered in the order of the sources
    cited = len(res["sources"])
    assert cited > 5 and [s["n"] for s in res["sources"]] == list(range(1, cited + 1))
    assert res["answer"] == "Answer combined from the notes " + "".join(f"[{n}]" for n in range(1, cited + 1)) + "."

    # a part that fails leaves a note, and the answer is written with the others
    control.write_text(json.dumps({"chat_fail_if": ["› 2.2 Function group 2.2"]}))
    res = idx.answer(question)
    assert res["failed_parts"] and len(res["failed_parts"]) < parts
    assert all(f.startswith(f"part ") and f" of {parts} failed: " in f for f in res["failed_parts"])
    assert res["answer"].endswith(f"({res['failed_parts'][-1]})") and 0 < len(res["sources"]) < cited
    control.write_text(json.dumps({"chat_fail_if": ["Sources:"]}))   # all of them: the error
    with pytest.raises(Exception, match="400"):
        idx.answer(question)


def test_the_last_request_of_the_notes_always_fits():
    from atlas.index.search import _combine, _renumber
    asked = []

    class Llm:
        def ask_text(self, system, user):
            asked.append(user)
            return "Merged [3]." if "merge" in system else "Answer [3][7]."
    text, requests = _combine(Llm(), "q", ["a [3] " + "x" * 700, "b [7] " + "y" * 700], 1000)
    assert (text, requests) == ("Answer [3][7].", 1)            # no two fit together: each one cut
    assert len(asked[0].split("\n\nQuestion:")[0]) <= 1000 + len("Notes:\n\n")
    assert _renumber("A [3][7], B [7, 3] and [12].", 9) == ("A [1][2], B [2, 1] and [12].", {3: 1, 7: 2})


def test_the_summaries_choose_the_documents_of_a_broad_question(docs, tmp_path, fake_llm, fake_embeddings):
    from atlas.index.search import SUMMARY_KINDS, _scope_hits
    only_module = Index(tmp_path / "module.db", fake_embeddings, fake_llm[0])
    only_module.add_module("SRS_TMS", MODULE)
    idx = Index(tmp_path / "atlas.db", fake_embeddings, fake_llm[0], {"scope_documents": 1})
    for data in docs.values():
        _add(idx, data)
    idx.add_module("SRS_TMS", MODULE)
    question = "Explain how tracks are deleted"
    hits = _scope_hits(idx.store, idx.embedder, question, 8, None, 3, None)
    assert hits and {h["kind"] for h in hits} <= set(SUMMARY_KINDS)    # not the passages of the module
    res = idx.answer(question)
    assert res["scope"] == "whole" and res["documents"] == ["spec_with_bookmarks.pdf"]    # scope_documents = 1
    # without summaries, the hits of the question choose
    assert {h["kind"] for h in _scope_hits(only_module.store, only_module.embedder, question, 8, None, 3, None)} \
        == {"requirement", "chunk"}
    assert only_module.answer(question)["documents"] == ["SRS_TMS"]
    assert only_module.answer(question, dry_run=True)["documents"] == ["SRS_TMS"]


def test_records_of_a_code_unit(samples):
    """An Ada package is one unit: the comment and the requirements of its spec go with the
    code of its body."""
    pytest.importorskip("tree_sitter_ada")
    from dataclasses import asdict
    from atlas.code import AdaAnalyzer
    from atlas.index import code_records
    files = [samples / "ada" / "src" / f for f in ("tracks.ads", "tracks.adb")]
    parts = [asdict(AdaAnalyzer({}).analyze_file(f, f.read_bytes())) for f in files]
    sources = {str(f): f.read_text() for f in files}
    records = {r["ref"]: r for r in code_records("tracks", parts, sources)}
    assert set(records) == {"tracks.update", "tracks.delete_track", "tracks.track", "tracks.track_array",
                            "tracks.hour_range", "tracks.trk_db", "tracks.max_tracks"}
    delete = records["tracks.delete_track"]
    assert delete["kind"] == "code_doc" and delete["section_title"] == "tracks.delete_track · tracks.adb:11-15"
    assert delete["text"].splitlines()[:3] == ["tracks.delete_track (subprogram, tracks.adb:11-15)",
                                               "Removes track I from the database.", "Req: SRS-0004"]
    assert 'Logger.Log ("delete");' in delete["text"] and delete["embed_text"].startswith("ada unit tracks > subprogram")
    assert records["tracks.max_tracks"]["text"].startswith("tracks.max_tracks (constant, tracks.ads:11)")
    # long code is cut; a test file gives tests; no code_doc in kinds, no records
    assert code_records("tracks", parts, sources, chunk_chars=40)[0]["text"].endswith("…")
    assert {r["text"].split(" (")[1].split(",")[0] for r in code_records("tracks", parts, sources, tests=[str(files[1])])
            if "tracks.adb" in r["text"]} == {"test"}
    assert code_records("tracks", parts, sources, kinds=["chunk"]) == []
