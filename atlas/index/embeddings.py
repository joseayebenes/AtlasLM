"""Embeddings through the [embeddings] connection (proxy, TLS, rate limit, retries).

Vectors are normalised, so the dot product is the cosine similarity. Some models expect a
prefix that tells queries from documents; it is added here and is part of the cache key.
"""
from __future__ import annotations

import hashlib

import numpy as np

from atlas.llm.connection import ServiceConfig, create_client

__all__ = ["EMBED_PREFIXES", "Embedder"]

# (query prefix, document prefix) by model name
EMBED_PREFIXES = {
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
    "nomic-embed": ("search_query: ", "search_document: "),
    "e5": ("query: ", "passage: "),
}


class Embedder:
    def __init__(self, cfg: ServiceConfig):
        self.client, self.proxy = create_client(cfg)
        self.model = cfg.model
        self.batch = cfg.batch
        self.q_prefix, self.d_prefix = next(
            (v for k, v in EMBED_PREFIXES.items() if k in self.model.lower()), ("", ""))

    def _embed(self, texts: list[str]) -> np.ndarray:
        vecs = []
        for i in range(0, len(texts), self.batch):
            resp = self.client.embeddings.create(model=self.model, input=texts[i:i + self.batch])
            vecs.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        m = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(norms == 0, 1, norms)

    def key(self, text: str) -> str:
        """Cache key of a document vector: it depends on the model, the prefix and the text."""
        return hashlib.sha256(f"{self.model}\x00{self.d_prefix}{text}".encode("utf-8")).hexdigest()[:32]

    def documents(self, texts: list[str]) -> np.ndarray:
        return self._embed([self.d_prefix + t for t in texts])

    def queries(self, texts: list[str]) -> np.ndarray:
        """Several queries in one request (it matters with a requests-per-minute limit)."""
        return self._embed([self.q_prefix + t for t in texts])
