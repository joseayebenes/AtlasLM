"""Index and hybrid search over the documents (legacy/doc_rag.py).

    records.py      search records of a document (overview, sections, chunks, requirements...)
    embeddings.py   Embedder: vectors through the [embeddings] connection
    store.py        IndexStore: records, vectors, FTS5 and embeddings cache in atlas.db
    search.py       hybrid search (IDs + vectors + BM25, merged with RRF) and cited answers

    from atlas.index import Index
    idx = Index.from_config(cfg)                    # the atlas.db of the workspace
    hits = idx.search("track deletion latency", k=8)
    res = idx.answer("How long does a track take to be deleted?")
    print(res["answer"], [s["cite"] for s in res["sources"]])
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path
from typing import Iterable

import numpy as np

from atlas.index.embeddings import Embedder
from atlas.index.records import KINDS, build_records, chunk_text
from atlas.index.search import answer, build_context, cite, search
from atlas.index.store import IndexStore
from atlas.llm.connection import ServiceConfig, diagnose, is_connection_error
from atlas.llm.structured import JsonLlm
from atlas.pipeline import FatalError

__all__ = ["KINDS", "Embedder", "Index", "IndexStore", "answer", "build_context", "build_records",
           "chunk_text", "cite", "index_document", "search"]

log = logging.getLogger(__name__)


def index_document(store: IndexStore, embedder: Embedder, data: dict, doc: str, name: str, out_dir: str = "",
                   chunk_chars: int = 1200, chunk_overlap: int = 200,
                   kinds: Iterable[str] | None = None) -> dict:
    """Embeds the records of a document (structure.json of atlas.docs) in batches, from the
    cache when possible, and replaces the document in the index. A failed batch does not
    stop the others: what has a vector is indexed and the result says what is missing.
    A connection error raises FatalError (what was embedded stays in the cache)."""
    if not data["sections"] or "body" not in data["sections"][0]:
        raise ValueError(f"{name}: structure.json without the text of the sections")
    records = build_records(data, chunk_chars, chunk_overlap, kinds)
    keys = [embedder.key(r["embed_text"]) for r in records]
    vectors = store.cache_get(keys)
    missing = [i for i, k in enumerate(keys) if k not in vectors]
    batches = [missing[i:i + embedder.batch] for i in range(0, len(missing), embedder.batch)]
    log.info("embeddings of '%s': %d records, %d in the cache, %d to compute in %d batches (%s)",
             name, len(records), len(records) - len(missing), len(missing), len(batches), embedder.model)
    failed, t0 = [], time.time()
    for n, batch in enumerate(batches, 1):
        try:
            new = dict(zip((keys[i] for i in batch), embedder.documents([records[i]["embed_text"] for i in batch])))
            store.cache_put(new)                 # saved batch by batch
            vectors.update(new)
            log.info("  batch %d/%d ok (%.0fs)", n, len(batches), time.time() - t0)
        except Exception as e:
            if is_connection_error(e):
                raise FatalError(f"connection error with the embeddings: {diagnose(e)}") from e
            failed.append({"batch": n, "records": len(batch), "error": f"{type(e).__name__}: {e}"[:500],
                           "kinds": sorted({records[i]["kind"] for i in batch}),
                           "sections": sorted({records[i].get("section_id", "") for i in batch} - {""})})
            log.warning("  batch %d/%d failed: %s", n, len(batches), e)

    ok = [i for i, k in enumerate(keys) if k in vectors]
    store.replace_document({"doc": doc, "name": name, "title": data["title"], "out_dir": out_dir,
                            "overview": data.get("overview", {}), "embed_model": embedder.model},
                           [records[i] for i in ok],
                           np.vstack([vectors[keys[i]] for i in ok]) if ok else np.zeros((0, 1), np.float32))
    kinds_done = Counter(records[i]["kind"] for i in ok)
    log.info("indexed '%s': %s%s", name, dict(kinds_done),
             f"; {len(records) - len(ok)} records without a vector in {len(failed)} batches" if failed else "")
    return {"records": len(ok), "missing": len(records) - len(ok), "failed_batches": failed,
            "kinds": dict(kinds_done)}


class Index:
    """The index of a workspace: add or remove documents, search and answer. The model
    (`llm`) is only needed to rewrite queries and to answer, and it is created on first use."""

    def __init__(self, db: str | Path, embeddings: ServiceConfig | Embedder,
                 llm: ServiceConfig | JsonLlm | None = None):
        self.store = IndexStore(db)
        self.embedder = embeddings if isinstance(embeddings, Embedder) else Embedder(embeddings)
        self._llm = llm

    @classmethod
    def from_config(cls, cfg) -> "Index":
        """The index of <workspace>/atlas.db with the [embeddings] and [llm] of atlas.toml."""
        from atlas.llm import from_config
        conn = from_config(cfg)
        return cls(cfg.workspace / "atlas.db", conn.embeddings, conn.llm)

    @property
    def llm(self) -> JsonLlm:
        if self._llm is None:
            raise ValueError("this Index has no model: pass llm=ServiceConfig or JsonLlm")
        if isinstance(self._llm, ServiceConfig):
            self._llm = JsonLlm(self._llm)
        return self._llm

    def add_document(self, data: dict, doc: str, name: str, out_dir: str = "", **options) -> dict:
        """See index_document (options: chunk_chars, chunk_overlap, kinds)."""
        return index_document(self.store, self.embedder, data, doc, name, out_dir, **options)

    def remove_document(self, doc: str) -> None:
        self.store.delete_document(doc)

    def documents(self) -> list[dict]:
        return self.store.documents()

    def search(self, query: str, k: int = 8, document: str = "", kinds: list[str] | None = None,
               rewrite: bool = False) -> list[dict]:
        return search(self.store, self.embedder, query, k, document, kinds, self.llm if rewrite else None)

    def answer(self, question: str, k: int = 8, document: str = "", rewrite: bool = True) -> dict:
        return answer(self.store, self.embedder, self.llm, question, k, document, rewrite)

    def requirement(self, req_id: str) -> list[dict]:
        """The exact text of a requirement by its ID, in every indexed document."""
        return self.store.requirement(req_id)
