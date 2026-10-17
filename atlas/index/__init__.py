"""Index and hybrid search over the documents (legacy/doc_rag.py).

    records.py      search records of a document (overview, sections, chunks, requirements...),
                    of a DOORS module (its requirements) and of a code unit (its elements)
    embeddings.py   Embedder: vectors through the [embeddings] connection
    store.py        IndexStore: records, vectors, FTS5 and embeddings cache in atlas.db
    search.py       hybrid search (IDs + vectors + BM25, merged with RRF) and cited answers
    tools.py        DocTools: the tools of agents and of the MCP server over the documents

    from atlas.index import Index
    idx = Index.from_config(cfg)                    # the atlas.db of the workspace
    idx.embed_missing(texts, workers=4)             # optional: many documents' texts in full batches
    hits = idx.search("track deletion latency")     # [search] results, per_document
    res = idx.answer("How long does a track take to be deleted?")
    print(res["answer"], [s["cite"] for s in res["sources"]])
    idx.answer("Summarise the track management requirements", scope="whole")   # whole documents

A local-only document (`local=True`, a source with model = "none") is indexed without
vectors: none of its texts goes to the embeddings model. Search finds it by IDs and BM25,
and answer leaves it out of what it sends to the model (see search.answer).
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np

from atlas.index.embeddings import Embedder
from atlas.index.records import (CODE_PREFIX, DOORS_PREFIX, KINDS, build_records, chunk_text, code_records,
                                  requirement_records)
from atlas.index.search import answer, answer_whole, build_context, cite, search
from atlas.index.store import IndexStore
from atlas.index.tools import DocTools
from atlas.llm.connection import ServiceConfig, diagnose, is_connection_error
from atlas.llm.structured import JsonLlm
from atlas.outbound import LocalOnly, record_sent, source_label
from atlas.pipeline import FatalError

__all__ = ["CODE_PREFIX", "DOORS_PREFIX", "KINDS", "DocTools", "Embedder", "Index", "IndexStore", "answer",
           "answer_whole", "build_context", "build_records", "chunk_text", "cite", "code_records", "embed_missing",
           "index_code", "index_document", "index_module", "requirement_records", "search"]

log = logging.getLogger(__name__)


def index_document(store: IndexStore, embedder: Embedder, data: dict, doc: str, name: str, out_dir: str = "",
                   chunk_chars: int = 1200, chunk_overlap: int = 200,
                   kinds: Iterable[str] | None = None, local: bool = False) -> dict:
    """Embeds the records of a document (structure.json of atlas.docs) in batches, from the
    cache when possible, and replaces the document in the index. A failed batch does not
    stop the others: what has a vector is indexed and the result says what is missing.
    A connection error raises FatalError (what was embedded stays in the cache). A
    local-only document (`local`) is indexed without vectors: nothing is embedded."""
    if not data["sections"] or "body" not in data["sections"][0]:
        raise ValueError(f"{name}: structure.json without the text of the sections")
    return _index_records(store, embedder, build_records(data, chunk_chars, chunk_overlap, kinds),
                          {"doc": doc, "name": name, "title": data["title"], "out_dir": out_dir,
                           "overview": data.get("overview", {})}, local)


def index_module(store: IndexStore, embedder: Embedder, module: str, requirements: list[dict],
                 kinds: Iterable[str] | None = None, local: bool = False) -> dict:
    """Indexes the requirements of a DOORS module (Store.requirements) as one more document,
    "doors:<module>", named after the module. Same batches, cache and result as
    index_document."""
    records = requirement_records(module, requirements, kinds)
    summary = f"DOORS module {module}: {sum(r['kind'] == 'requirement' for r in records)} requirements"
    return _index_records(store, embedder, records, {"doc": DOORS_PREFIX + module, "name": module,
                                                     "title": f"{module} (DOORS)", "out_dir": "",
                                                     "overview": {"summary": summary}}, local)


def index_code(store: IndexStore, embedder: Embedder, unit: str, parts: list[dict], sources: dict[str, str],
               tests: Iterable[str] = (), chunk_chars: int = 1200, kinds: Iterable[str] | None = None,
               local: bool = False) -> dict:
    """Indexes a code unit (its files, as code.parse leaves them; see code_records) as one
    more document, "code:<unit>". Same batches, cache and result as index_document."""
    records = code_records(unit, parts, sources, tests, chunk_chars, kinds)
    files = ", ".join(sorted(p["file"] for p in parts))
    return _index_records(store, embedder, records, {"doc": CODE_PREFIX + unit, "name": unit,
                                                     "title": f"{unit} (code)", "out_dir": "",
                                                     "overview": {"summary": f"code unit {unit}: {files}"}}, local)


def embed_missing(store: IndexStore, embedder: Embedder, texts: Iterable[str], workers: int = 1,
                  sources: Mapping[str, str] | None = None) -> dict:
    """Embeds the texts that are not in the cache yet, in full batches of `embedder.batch`,
    `workers` batches at a time, and saves each batch in the cache. The texts of many
    documents go in the same batches: fewer requests than embedding each document on its
    own (index_document then finds everything in the cache). Returns how many texts there
    were, how many were in the cache, how many were embedded and the failed batches. A
    connection error raises FatalError; the batches that finished stay in the cache.
    `sources` gives the source of each text (atlas.outbound.source_label), for the record
    of what was sent: a batch counts once it reached the server, even if it failed."""
    by_key: dict[str, str] = {}
    for t in texts:
        by_key.setdefault(embedder.key(t), t)               # each distinct text once
    cached = store.cache_get(list(by_key))
    missing = [(k, t) for k, t in by_key.items() if k not in cached]
    batches = [missing[i:i + embedder.batch] for i in range(0, len(missing), embedder.batch)]
    out = {"texts": len(by_key), "cached": len(cached), "embedded": 0, "failed_batches": []}
    if not batches:
        return out
    log.info("embeddings: %d texts, %d in the cache, %d to compute in %d batches of up to %d (%s)",
             len(by_key), len(cached), len(missing), len(batches), embedder.batch, embedder.model)

    def one(batch: list[tuple[str, str]]) -> int:
        try:
            store.cache_put(dict(zip((k for k, _ in batch), embedder.documents([t for _, t in batch]))))
        except Exception as e:
            if not is_connection_error(e):
                _record_texts(store, embedder, [(sources or {}).get(t, "") for _, t in batch])
            raise
        _record_texts(store, embedder, [(sources or {}).get(t, "") for _, t in batch])
        return len(batch)

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = {pool.submit(one, b): n for n, b in enumerate(batches, 1)}
        for f in as_completed(futures):
            try:
                out["embedded"] += f.result()
            except Exception as e:
                if is_connection_error(e):
                    raise FatalError(f"connection error with the embeddings: {diagnose(e)}") from e
                out["failed_batches"].append({"batch": futures[f], "error": f"{type(e).__name__}: {e}"[:500]})
                log.warning("  batch %d/%d failed: %s", futures[f], len(batches), e)
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)      # the running batches end in the cache
        raise
    pool.shutdown()
    return out


def _record_texts(store: IndexStore, embedder: Embedder, labels: list[str]) -> None:
    """The record of the texts sent to the embeddings model, by source ("?" if unknown)."""
    record_sent(store.path, embedder.model, Counter(label or "?" for label in labels), "texts")


def _index_records(store: IndexStore, embedder: Embedder, records: list[dict], doc: dict,
                   local: bool = False) -> dict:
    name = doc["name"]
    if local:                                # nothing of it goes to the embeddings model
        store.replace_document({**doc, "embed_model": "", "local": True}, records, None)
        kinds = dict(Counter(r["kind"] for r in records))
        log.info("indexed '%s' as local-only, without vectors: %s", name, kinds)
        return {"records": len(records), "missing": 0, "failed_batches": [], "kinds": kinds, "local": True}
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
            _record_texts(store, embedder, [source_label(doc["doc"])] * len(batch))
            store.cache_put(new)                 # saved batch by batch
            vectors.update(new)
            log.info("  batch %d/%d ok (%.0fs)", n, len(batches), time.time() - t0)
        except Exception as e:
            if is_connection_error(e):
                raise FatalError(f"connection error with the embeddings: {diagnose(e)}") from e
            _record_texts(store, embedder, [source_label(doc["doc"])] * len(batch))
            failed.append({"batch": n, "records": len(batch), "error": f"{type(e).__name__}: {e}"[:500],
                           "kinds": sorted({records[i]["kind"] for i in batch}),
                           "sections": sorted({records[i].get("section_id", "") for i in batch} - {""})})
            log.warning("  batch %d/%d failed: %s", n, len(batches), e)

    ok = [i for i, k in enumerate(keys) if k in vectors]
    store.replace_document({**doc, "embed_model": embedder.model}, [records[i] for i in ok],
                           np.vstack([vectors[keys[i]] for i in ok]) if ok else np.zeros((0, 1), np.float32))
    kinds_done = Counter(records[i]["kind"] for i in ok)
    log.info("indexed '%s': %s%s", name, dict(kinds_done),
             f"; {len(records) - len(ok)} records without a vector in {len(failed)} batches" if failed else "")
    return {"records": len(ok), "missing": len(records) - len(ok), "failed_batches": failed,
            "kinds": dict(kinds_done)}


class Index:
    """The index of a workspace: add or remove documents, search and answer. The model
    (`llm`) is only needed to rewrite queries and to answer, and it is created on first use.
    `settings` are those of [search] (results, per_document, context_chars, scope,
    scope_chars); what is not given takes its default."""

    def __init__(self, db: str | Path, embeddings: ServiceConfig | Embedder,
                 llm: ServiceConfig | JsonLlm | None = None, settings: dict | None = None,
                 local: LocalOnly | None = None):
        from atlas.config import DEFAULTS
        self.store = IndexStore(db)
        self.local_only = local or LocalOnly()
        self.embedder = embeddings if isinstance(embeddings, Embedder) else Embedder(embeddings)
        self._llm = llm
        self.settings = {**DEFAULTS["search"], **(settings or {})}

    @classmethod
    def from_config(cls, cfg) -> "Index":
        """The index of <workspace>/atlas.db with the [embeddings], [llm] and [search] of
        atlas.toml, and its local-only sources."""
        from atlas.llm import from_config
        conn = from_config(cfg)
        return cls(cfg.workspace / "atlas.db", conn.embeddings, conn.llm, cfg.get("search"), LocalOnly.from_config(cfg))

    def is_local(self, doc: str) -> bool:
        """Whether a document of the index (its key) is local-only: indexed as such, or of a
        source that the configuration keeps local now."""
        return self.local_only.index_doc(doc) or doc in self.store.local_documents()

    def local_filter(self):
        """is_local for many documents: the indexed ones are read once."""
        indexed = self.store.local_documents()
        return lambda doc: doc in indexed or self.local_only.index_doc(doc)

    @property
    def llm(self) -> JsonLlm:
        if self._llm is None:
            raise ValueError("this Index has no model: pass llm=ServiceConfig or JsonLlm")
        if isinstance(self._llm, ServiceConfig):
            self._llm = JsonLlm(self._llm)
        return self._llm

    def add_document(self, data: dict, doc: str, name: str, out_dir: str = "", **options) -> dict:
        """See index_document (options: chunk_chars, chunk_overlap, kinds, local)."""
        return index_document(self.store, self.embedder, data, doc, name, out_dir, **options)

    def add_module(self, module: str, requirements: list[dict], kinds: Iterable[str] | None = None,
                   local: bool = False) -> dict:
        """See index_module."""
        return index_module(self.store, self.embedder, module, requirements, kinds, local)

    def add_code(self, unit: str, parts: list[dict], sources: dict[str, str], tests: Iterable[str] = (),
                 chunk_chars: int = 1200, kinds: Iterable[str] | None = None, local: bool = False) -> dict:
        """See index_code."""
        return index_code(self.store, self.embedder, unit, parts, sources, tests, chunk_chars, kinds, local)

    def embed_missing(self, texts: Iterable[str], workers: int = 1, sources: Mapping[str, str] | None = None) -> dict:
        """See embed_missing."""
        return embed_missing(self.store, self.embedder, texts, workers, sources)

    def remove_document(self, doc: str) -> None:
        self.store.delete_document(doc)

    def documents(self) -> list[dict]:
        return self.store.documents()

    def search(self, query: str, k: int = 0, document: str = "", kinds: list[str] | None = None,
               rewrite: bool = False, dense: bool = True, per_document: int | None = None) -> list[dict]:
        """k = 0 and per_document = None: those of the settings."""
        per = self.settings["per_document"] if per_document is None else per_document
        return search(self.store, self.embedder, query, k or self.settings["results"], document, kinds,
                      self.llm if rewrite else None, dense, per)

    def answer(self, question: str, k: int = 0, document: str = "", rewrite: bool = True,
               kinds: list[str] | None = None, scope: str = "") -> dict:
        """scope: passages | whole | auto ("" = that of the settings); see search.answer."""
        s = self.settings
        return answer(self.store, self.embedder, self.llm, question, k or s["results"], document, rewrite, kinds,
                      s["per_document"], s["context_chars"], scope or s["scope"], s["scope_chars"],
                      self.local_filter())

    def requirement(self, req_id: str) -> list[dict]:
        """The exact text of a requirement by its ID, in every indexed document."""
        return self.store.requirement(req_id)

