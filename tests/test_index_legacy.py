"""atlas.index gives the same records, search results and answer sources as
legacy/doc_rag.py on the sample documents, with the fake API as the model and the
embeddings. To be deleted together with legacy/."""
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from atlas.docs import DocsOptions, process_document
from atlas.index import Index, build_records

LEGACY = Path(__file__).resolve().parents[1] / "legacy"
pytestmark = pytest.mark.skipif(not (LEGACY / "doc_rag.py").exists(), reason="legacy/ has been removed")

QUERIES = ["SRS-0102", "alarm when a track is lost", "keep logs for 30 days", "track event 3 of group 2.3.2",
           "Function group 1.2 latency"]
FIELDS = ("doc_name", "kind", "section_id", "ref", "seq", "text", "page_start", "page_end")


@pytest.fixture(scope="module")
def legacy():
    pytest.importorskip("markitdown")
    sys.path.insert(0, str(LEGACY))
    try:
        import conexion
        import doc_rag
        import pdf_struct
        yield doc_rag, pdf_struct, conexion
    finally:
        sys.path.remove(str(LEGACY))


def test_same_records_search_and_sources_as_doc_rag(legacy, docs_samples, tmp_path, fake_llm, fake_embeddings):
    rag, ps, cx = legacy
    llm = fake_llm[0]
    net = cx.NetConfig(proxy="", no_proxy="*")
    old_llm = cx.ServiceConfig(base_url=llm.base_url, model="fake", api_key="x", net=net)
    old_emb = cx.ServiceConfig(base_url=llm.base_url, model="embeddinggemma", api_key="x",
                               batch=fake_embeddings.batch, net=net)
    old_store, old_embedder = rag.Store(str(tmp_path / "rag.db")), rag.Embedder(old_emb)
    new = Index(tmp_path / "atlas.db", replace(fake_embeddings, document_prefix="title: none | text: "), llm,
                {"per_document": 0, "scope": "passages"})                # what doc_rag.py did

    for f in ("spec_with_bookmarks.pdf", "spec_without_bookmarks.pdf", "hmi_manual.md"):
        data = process_document(docs_samples / f, tmp_path / "docs" / f, DocsOptions(), llm=llm)
        assert build_records(data) == rag.construir_registros(data)
        rag.indexar(old_store, old_embedder, dict(data), f)
        new.add_document(data, doc=f"docs:{f}", name=f, out_dir=data["out_dir"])

    old_llm_client = ps.LLM(old_llm)
    for q in QUERIES:
        for rewrite in (False, True):
            old = rag.buscar(old_store, old_embedder, q, 8, llm=old_llm_client if rewrite else None)
            hits = new.search(q, 8, rewrite=rewrite)
            assert [{k: h.get(k) for k in FIELDS} for h in hits] == [{k: h.get(k) for k in FIELDS} for h in old], q
        for document, kinds in (("hmi_manual.md", None), ("", ["requirement"]), ("Track Management", ["chunk"])):
            old = rag.buscar(old_store, old_embedder, q, 5, document, kinds)
            assert [h["text"] for h in new.search(q, 5, document, kinds)] == [h["text"] for h in old], (q, document)

    question = "When does the HMI raise an alarm?"
    old = rag.responder(old_store, old_embedder, old_llm_client, question)
    res = new.answer(question)
    assert res["answer"] == old["answer"]
    # the cited ones and the rest; the context is wider than in doc_rag.py ([search] context_radius):
    # a section summary brings the chunks of its section, which later hits would have repeated,
    # so fewer sources go out
    sent = sorted(res["sources"] + res["unused"], key=lambda s: s["n"])
    new_cites, old_cites = [s["cite"] for s in sent], [s["cite"] for s in old["sources"]]
    assert new_cites[0] == old_cites[0] and len(new_cites) <= len(old_cites)
    assert set(new_cites) - set(old_cites) <= {c.replace("(chunk)", "(section_summary)") for c in old_cites}
