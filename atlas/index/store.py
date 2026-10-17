"""Index tables in the workspace atlas.db: records with their vectors, FTS5 (BM25) over the
same records, and the embeddings cache.

    index_documents   one row per indexed document: key, name, title, output folder,
                      overview and the embeddings model of its vectors
    index_records     the records (see records.py) with their vector and their meta (JSON:
                      what a search can be narrowed by, see allowed)
    index_fts         full-text index of embed_text and ref (if SQLite has FTS5)
    embed_cache       vector by key (model + prefix + text): what has been embedded once
                      is never embedded again, whatever document it comes from

A document is replaced in a single transaction. Dense search only uses vectors of the
current embeddings model, so changing the model never mixes vectors of two models.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

from atlas.index.records import asks_for

__all__ = ["IndexStore"]

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_documents (
    doc TEXT PRIMARY KEY, name TEXT, title TEXT, out_dir TEXT, overview TEXT,
    embed_model TEXT, indexed_at TEXT);
CREATE TABLE IF NOT EXISTS index_records (
    id INTEGER PRIMARY KEY, doc TEXT, kind TEXT, section_id TEXT, section_title TEXT, path TEXT,
    page_start INTEGER, page_end INTEGER, ref TEXT, seq INTEGER, text TEXT, embed_text TEXT, vector BLOB,
    meta TEXT);
CREATE INDEX IF NOT EXISTS idx_index_records_doc ON index_records(doc);
CREATE INDEX IF NOT EXISTS idx_index_records_ref ON index_records(ref);
CREATE TABLE IF NOT EXISTS embed_cache (key TEXT PRIMARY KEY, vector BLOB);
"""

_RECORD_COLUMNS = "r.id, r.doc, r.kind, r.section_id, r.section_title, r.path, r.page_start, r.page_end, " \
                  "r.ref, r.seq, r.text, r.embed_text, r.meta"


def _record(row: sqlite3.Row) -> dict:
    return {**dict(row), "meta": json.loads(row["meta"] or "{}")}


def _chapter_is(chapter: str | None, wanted: str) -> bool:
    return bool(chapter) and asks_for(wanted, chapter)


class IndexStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        if "meta" not in {r["name"] for r in self.db.execute("PRAGMA table_info(index_records)")}:
            self.db.execute("ALTER TABLE index_records ADD COLUMN meta TEXT")   # an index of before 0.3
        self.db.create_function("atlas_chapter_is", 2, _chapter_is, deterministic=True)
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS index_fts USING fts5("
                            "embed_text, ref, tokenize='unicode61 remove_diacritics 2')")
            self.fts = True
        except sqlite3.OperationalError:
            log.warning("this SQLite has no FTS5: vector search only")
            self.fts = False
        self.lock = threading.RLock()     # one connection shared by the Runner's threads
        self._matrix: tuple[str, int, np.ndarray, np.ndarray] | None = None

    def _query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    # --- embeddings cache ---------------------------------------------------
    def cache_get(self, keys: list[str]) -> dict[str, np.ndarray]:
        out = {}
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            for key, blob in self._query(f"SELECT key, vector FROM embed_cache WHERE key IN "
                                         f"({','.join('?' * len(part))})", part):
                out[key] = np.frombuffer(blob, dtype=np.float32)
        return out

    def cache_put(self, items: dict[str, np.ndarray]) -> None:
        with self.lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO embed_cache VALUES (?, ?)",
                                [(k, v.astype(np.float32).tobytes()) for k, v in items.items()])

    # --- documents ----------------------------------------------------------
    def _delete(self, doc: str) -> None:
        if self.fts:
            self.db.execute("DELETE FROM index_fts WHERE rowid IN (SELECT id FROM index_records WHERE doc = ?)",
                            (doc,))
        self.db.execute("DELETE FROM index_records WHERE doc = ?", (doc,))
        self.db.execute("DELETE FROM index_documents WHERE doc = ?", (doc,))

    def delete_document(self, doc: str) -> None:
        with self.lock, self.db:
            self._delete(doc)
        self._matrix = None

    def replace_document(self, doc: dict, records: list[dict], vectors: np.ndarray) -> None:
        """`doc`: doc (key), name, title, out_dir, overview, embed_model. All or nothing."""
        with self.lock, self.db:
            self._delete(doc["doc"])
            self.db.execute("INSERT INTO index_documents VALUES (?,?,?,?,?,?,?)",
                            (doc["doc"], doc["name"], doc["title"], doc.get("out_dir", ""),
                             json.dumps(doc.get("overview") or {}, ensure_ascii=False), doc["embed_model"],
                             datetime.now().isoformat(timespec="seconds")))
            for r, v in zip(records, vectors):
                cur = self.db.execute(
                    "INSERT INTO index_records (doc, kind, section_id, section_title, path, page_start, "
                    "page_end, ref, seq, text, embed_text, vector, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc["doc"], r["kind"], r.get("section_id", ""), r.get("section_title", ""),
                     r.get("path", ""), r.get("page_start", 0), r.get("page_end", 0), r.get("ref", ""),
                     r.get("seq", 0), r["text"], r["embed_text"], v.astype(np.float32).tobytes(),
                     json.dumps(r.get("meta") or {}, ensure_ascii=False)))
                if self.fts:
                    self.db.execute("INSERT INTO index_fts (rowid, embed_text, ref) VALUES (?, ?, ?)",
                                    (cur.lastrowid, r["embed_text"], r.get("ref", "")))
        self._matrix = None

    def documents(self) -> list[dict]:
        return [{**dict(r), "overview": json.loads(r["overview"] or "{}")} for r in self._query(
            "SELECT d.*, COUNT(r.id) AS records FROM index_documents d LEFT JOIN index_records r "
            "ON r.doc = d.doc GROUP BY d.doc ORDER BY d.name")]

    def counts(self) -> dict[str, int]:
        return {"documents": self._query("SELECT COUNT(*) FROM index_documents")[0][0],
                "records": self._query("SELECT COUNT(*) FROM index_records")[0][0],
                "cached_vectors": self._query("SELECT COUNT(*) FROM embed_cache")[0][0]}

    # --- search -------------------------------------------------------------
    def _load_matrix(self, model: str) -> tuple[np.ndarray, np.ndarray]:
        """The vectors of a model, loaded again when the index changed: through this store
        (which drops them) or through another connection, such as an `atlas run` while a
        long-lived process (the MCP server) searches. data_version only changes with the
        commits of other connections."""
        with self.lock:
            version = self.db.execute("PRAGMA data_version").fetchone()[0]
            if self._matrix is None or self._matrix[:2] != (model, version):
                rows = self.db.execute("SELECT r.id, r.vector FROM index_records r JOIN index_documents d "
                                       "ON d.doc = r.doc WHERE d.embed_model = ? ORDER BY r.id", (model,)).fetchall()
                ids = np.array([r["id"] for r in rows], dtype=np.int64)
                m = (np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
                     if rows else np.zeros((0, 1), dtype=np.float32))
                self._matrix = (model, version, ids, m)
            return self._matrix[2], self._matrix[3]

    def allowed(self, document: str = "", kinds: list[str] | None = None,
                where: dict[str, str] | None = None) -> set[int] | None:
        """Record ids of a document (by name, key or part of its title), of some kinds and
        whose meta has every key = value of `where` (in any case; `chapter` also by its
        number or its title without the number); None if there is no filter."""
        if not document and not kinds and not where:
            return None
        sql, params = "SELECT r.id FROM index_records r JOIN index_documents d ON d.doc = r.doc WHERE 1=1", []
        if document:
            sql += " AND (d.name = ? OR d.doc = ? OR d.title LIKE ?)"
            params += [document, document, f"%{document}%"]
        if kinds:
            sql += f" AND r.kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        for key, value in (where or {}).items():
            path = '$."' + str(key).lower().replace('"', "") + '"'
            if str(key).lower() == "chapter":
                sql += " AND atlas_chapter_is(json_extract(r.meta, ?), ?)"
            else:
                sql += " AND json_extract(r.meta, ?) = ? COLLATE NOCASE"
            params += [path, str(value).strip()]
        return {r[0] for r in self._query(sql, params)}

    def meta_values(self, limit: int = 20) -> dict[str, list[str]]:
        """Every key of the meta of the records, with some of its values (the most
        frequent first): what `where` can take."""
        rows = self._query("SELECT j.key, j.value, COUNT(*) AS n FROM index_records r, json_each(r.meta) j "
                           "GROUP BY j.key, j.value ORDER BY j.key, n DESC, j.value")
        out: dict[str, list[str]] = {}
        for r in rows:
            values = out.setdefault(r["key"], [])
            if len(values) < limit:
                values.append(str(r["value"]))
        return out

    def dense(self, qvec: np.ndarray, model: str, n: int, allowed: set[int] | None) -> list[int]:
        ids, m = self._load_matrix(model)
        if len(ids) == 0:
            return []
        scores = m @ qvec
        out = []
        for i in np.argsort(-scores):
            rid = int(ids[i])
            if allowed is None or rid in allowed:
                out.append(rid)
                if len(out) >= n:
                    break
        return out

    def lexical(self, query: str, n: int, allowed: set[int] | None) -> list[int]:
        if not self.fts:
            return []
        tokens = re.findall(r"[\w][\w\-./]*", query)
        if not tokens:
            return []
        # each token in quotes: IDs with hyphens are searched as an exact phrase
        q = " OR ".join('"' + t.replace('"', '') + '"' for t in tokens)
        try:
            rows = self._query("SELECT rowid FROM index_fts WHERE index_fts MATCH ? ORDER BY bm25(index_fts) "
                               "LIMIT ?", (q, n * 4))
        except sqlite3.OperationalError:
            return []
        return [r[0] for r in rows if allowed is None or r[0] in allowed][:n]

    def by_ref(self, query: str, allowed: set[int] | None) -> list[int]:
        """Exact match of the IDs (requirements, terms) mentioned in the query."""
        candidates = {t.upper() for t in re.findall(r"[A-Za-z0-9][\w\-.]*[\-_.\d][\w\-.]*", query)}
        if not candidates:
            return []
        rows = self._query(f"SELECT id FROM index_records WHERE UPPER(ref) IN ({','.join('?' * len(candidates))})",
                           list(candidates))
        return [r[0] for r in rows if allowed is None or r[0] in allowed]

    def records(self, ids: list[int]) -> list[dict]:
        """Records by id, in the given order, with the name, title and folder of their document."""
        if not ids:
            return []
        rows = self._query(f"SELECT {_RECORD_COLUMNS}, d.name AS doc_name, d.title AS doc_title, d.out_dir "
                           f"FROM index_records r JOIN index_documents d ON d.doc = r.doc "
                           f"WHERE r.id IN ({','.join('?' * len(ids))})", ids)
        by_id = {r["id"]: _record(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def document_records(self, doc: str) -> list[dict]:
        """Every record of a document (by its key), in the order it was indexed: that of the
        document."""
        return [_record(r) for r in self._query(
            f"SELECT {_RECORD_COLUMNS}, d.name AS doc_name, d.title AS doc_title, d.out_dir "
            f"FROM index_records r JOIN index_documents d ON d.doc = r.doc WHERE r.doc = ? ORDER BY r.id", (doc,))]

    def neighbours(self, rec: dict, radius: int = 1) -> list[dict]:
        """The adjacent chunks of the same section ('small-to-big' context)."""
        if rec["kind"] != "chunk":
            return [rec]
        rows = self._query("SELECT id FROM index_records WHERE doc = ? AND kind = 'chunk' AND section_id = ? "
                           "AND seq BETWEEN ? AND ? ORDER BY seq",
                           (rec["doc"], rec["section_id"], rec["seq"] - radius, rec["seq"] + radius))
        return self.records([r[0] for r in rows])

    def requirement(self, req_id: str) -> list[dict]:
        """Records of the requirement with that exact ID, in every document."""
        rows = self._query("SELECT id FROM index_records WHERE kind = 'requirement' AND UPPER(ref) = ?",
                           (req_id.strip().upper(),))
        return self.records([r[0] for r in rows])
