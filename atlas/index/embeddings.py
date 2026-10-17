"""Embeddings through the [embeddings] connection (proxy, TLS, rate limit, retries).

Vectors are normalised, so the dot product is the cosine similarity. Some models expect a
prefix that tells queries from documents; it is added here and is part of the cache key.
The prefixes come from the family of the model ([embeddings] family), inferred from its
name when not given, and the literal query_prefix / document_prefix replace those of the
family. A model of no known family gets none, and a warning says so: a gateway may publish
embeddinggemma under another name. "{title}" in a document prefix is the title of the
document (embeddinggemma: "title: {title} | text: ").
"""
from __future__ import annotations

import hashlib
import logging

import numpy as np

from atlas.llm.connection import ServiceConfig, create_client

__all__ = ["FAMILIES", "Embedder", "family", "prefix_note", "prefixes"]

log = logging.getLogger(__name__)

# (query prefix, document prefix) by family
FAMILIES = {
    "embeddinggemma": ("task: search result | query: ", "title: {title} | text: "),
    "nomic": ("search_query: ", "search_document: "),
    "e5": ("query: ", "passage: "),
    "none": ("", ""),
}
MODEL_FAMILIES = {"embeddinggemma": "embeddinggemma", "nomic-embed": "nomic", "e5": "e5"}   # part of the name
_warned: set[str] = set()


def family(cfg: ServiceConfig) -> str:
    """The family of the embeddings model: [embeddings] family, or the one its name says
    ("" if none)."""
    if cfg.family:
        if cfg.family not in FAMILIES:
            raise ValueError(f"[embeddings] family '{cfg.family}' is not one of {', '.join(FAMILIES)}")
        return cfg.family
    return next((f for name, f in MODEL_FAMILIES.items() if name in cfg.model.lower()), "")


def prefixes(cfg: ServiceConfig) -> tuple[str, str]:
    """(query prefix, document prefix) of the embeddings, without creating a client. A
    model of no known family and without literal prefixes gets none, with a warning in the
    log (once per model)."""
    fam = family(cfg)
    q, d = FAMILIES.get(fam, ("", ""))
    if not fam and not (cfg.query_prefix or cfg.document_prefix) and cfg.model not in _warned:
        _warned.add(cfg.model)
        log.warning(_unknown(cfg))
    return cfg.query_prefix or q, cfg.document_prefix or d


def _unknown(cfg: ServiceConfig) -> str:
    return (f"the embeddings model '{cfg.model}' is of no known family ({', '.join(MODEL_FAMILIES)} in its name): "
            f"no query or document prefix. If it is one of them under another name, set [embeddings] family "
            f"(embeddinggemma | nomic | e5); family = \"none\" if it needs no prefix")


def prefix_note(cfg: ServiceConfig) -> str:
    """The line of atlas check: the effective prefixes, or why there are none."""
    try:
        if not family(cfg) and not (cfg.query_prefix or cfg.document_prefix):
            return f"WARNING {_unknown(cfg)}"
        q, d = prefixes(cfg)
    except ValueError as e:
        return f"ERROR {e}"
    return f"embeddings prefixes: query={q!r} document={d!r}"


class Embedder:
    def __init__(self, cfg: ServiceConfig):
        self.q_prefix, self.d_prefix = prefixes(cfg)
        self.client, self.proxy = create_client(cfg)
        self.model = cfg.model
        self.batch = cfg.batch

    def _embed(self, texts: list[str]) -> np.ndarray:
        vecs = []
        for i in range(0, len(texts), self.batch):
            resp = self.client.embeddings.create(model=self.model, input=texts[i:i + self.batch])
            vecs.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        m = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(norms == 0, 1, norms)

    def document_text(self, text: str, title: str = "") -> str:
        """What is embedded for a document text: its prefix, with the title of its document."""
        return self.d_prefix.replace("{title}", " ".join(title.split()) or "none") + text

    def key(self, text: str, title: str = "") -> str:
        """Cache key of a document vector: it depends on the model, the prefix and the text."""
        return hashlib.sha256(f"{self.model}\x00{self.document_text(text, title)}".encode("utf-8")).hexdigest()[:32]

    def documents(self, texts: list[str], titles: list[str] | None = None) -> np.ndarray:
        """Document texts, each with the title of its document (`titles`, if the prefix
        uses it)."""
        return self._embed([self.document_text(t, ti) for t, ti in zip(texts, titles or [""] * len(texts))])

    def queries(self, texts: list[str]) -> np.ndarray:
        """Several queries in one request (it matters with a requests-per-minute limit)."""
        return self._embed([self.q_prefix + t for t in texts])
