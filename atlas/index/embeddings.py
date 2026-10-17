"""Embeddings through the [embeddings] connection (proxy, TLS, rate limit, retries).

Vectors are normalised, so the dot product is the cosine similarity. Some models expect a
prefix that tells queries from documents; it is added here and is part of the cache key.
"""
from __future__ import annotations

import hashlib

import numpy as np

from atlas.llm.connection import ServiceConfig, create_client

__all__ = ["EMBED_PREFIXES", "Embedder", "document_key", "prefixes"]

# (query prefix, document prefix) by model name
EMBED_PREFIXES = {
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
    "nomic-embed": ("search_query: ", "search_document: "),
    "e5": ("query: ", "passage: "),
}


def prefixes(model: str) -> tuple[str, str]:
    """(query prefix, document prefix) of an embeddings model; ("", "") if it needs none."""
    return next((v for k, v in EMBED_PREFIXES.items() if k in model.lower()), ("", ""))


def document_key(model: str, text: str) -> str:
    """Cache key of a document vector: a hash of the model, its document prefix and the
    text. The model is inside the hash, so a key of one model never matches a text
    embedded with another (and cannot be read back from the key)."""
    return hashlib.sha256(f"{model}\x00{prefixes(model)[1]}{text}".encode("utf-8")).hexdigest()[:32]


class Embedder:
    def __init__(self, cfg: ServiceConfig):
        self.client, self.proxy = create_client(cfg)
        self.model = cfg.model
        self.batch = cfg.batch
        self.q_prefix, self.d_prefix = prefixes(self.model)

    def _embed(self, texts: list[str]) -> np.ndarray:
        vecs = []
        for i in range(0, len(texts), self.batch):
            resp = self.client.embeddings.create(model=self.model, input=texts[i:i + self.batch])
            vecs.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        m = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(norms == 0, 1, norms)

    def key(self, text: str) -> str:
        """Cache key of a document vector: see document_key."""
        return document_key(self.model, text)

    def documents(self, texts: list[str]) -> np.ndarray:
        return self._embed([self.d_prefix + t for t in texts])

    def queries(self, texts: list[str]) -> np.ndarray:
        """Several queries in one request (it matters with a requests-per-minute limit)."""
        return self._embed([self.q_prefix + t for t in texts])
