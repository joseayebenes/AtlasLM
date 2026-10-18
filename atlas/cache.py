"""Caches shared between workstations: what one workstation asked the models, for the others.

`atlas run` asks the model once for each section (the analyses in <workspace>/docs/.cache)
and the embeddings once for each text (the embed_cache table of atlas.db). Both caches
are keyed by content, never by path: a section by its text, the chat model, the language
and the version of the prompt; a vector by the embeddings model, its prefix and the text.
So what one workspace computed serves any other workspace of the same project, on any
workstation. The workspace itself is never shared (one process at a time; atlas.lock).

    from atlas.cache import pack, unpack
    pack(cfg, "tms-cache.sqlite")                        # what this workspace computed
    pack(cfg, "tms-cache.sqlite", with_judgements=True)  # also those of verify, comply and link
    unpack(cfg, "tms-cache.sqlite")                      # adds what is missing, never replaces

The exported file is a SQLite database:

    header      key, value: format, atlas_version, project, chat_model, embeddings_model,
                created and counts (JSON)
    embeddings  key, vector, model: the rows of embed_cache of the embeddings model
    files       kind, name, content: the JSON of docs/.cache (kind "docs") and, with
                with_judgements, the caches of judgements of reports/ (kind "judgements")

The key of a vector is a hash of the embeddings model, its prefix and the text, so a key
of one model never matches a text embedded with another: importing the vectors of
another model would add rows that are never used. unpack refuses them.

[project] shared_cache names a folder (a network share) with exported caches (*.sqlite):
a second cache, read only, that the vectors (IndexStore.cache_get) and the analyses of
sections (atlas.docs.analysis) look up when the workspace lacks an entry. What is found
there is copied to the workspace; the folder is never written by atlas run (an
administrator fills it with `atlas cache export`). If the folder cannot be read, atlas
goes on without it, with a warning once.

    shared = shared_cache(cfg)       # SharedCache, or None without [project] shared_cache
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from atlas.config import Config

__all__ = ["CacheError", "FORMAT", "JUDGEMENT_FILES", "SharedCache", "docs_cache", "header", "pack", "read_only",
           "shared_cache", "unpack"]

log = logging.getLogger("atlas.cache")

FORMAT = "atlas-cache-1"
# The caches of judgements of reports/: atlas verify, comply and link
JUDGEMENT_FILES = (".verify_cache.json", ".comply_cache.json", ".link_cache.json")
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.json$")        # a file of docs/.cache, never a path
_KEY = re.compile(r"^[0-9a-f]{32}$")

SCHEMA = """
CREATE TABLE header (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE embeddings (key TEXT PRIMARY KEY, vector BLOB, model TEXT);
CREATE TABLE files (kind TEXT, name TEXT, content TEXT, PRIMARY KEY (kind, name));
"""


class CacheError(ValueError):
    """A file that is not an exported cache, or one that does not fit this configuration."""


def docs_cache(cfg: Config) -> Path:
    """The folder of the analyses of sections of a workspace."""
    return cfg.workspace / "docs" / ".cache"


def _models(cfg: Config) -> tuple[str, str]:
    from atlas.llm import from_config
    conn = from_config(cfg)
    return conn.llm.model, conn.embeddings.model


def _lock(cfg: Config):
    from atlas.pipeline import _WorkspaceLock
    return _WorkspaceLock(cfg.workspace / "atlas.lock")


def read_only(path: str | Path) -> sqlite3.Connection:
    """A read-only connection that never writes beside the file (no journal, no lock
    file): it works on a read-only network folder. The file must not change while it is
    open."""
    p = Path(path).absolute().as_posix()
    uri = "file://" + ("" if p.startswith("/") else "/") + quote(p, safe="/:") + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, check_same_thread=False)


def header(db: sqlite3.Connection, name: str = "") -> dict:
    """The header of an exported cache (counts as a dict). CacheError if it is not one,
    or one of a newer format."""
    try:
        rows = dict(db.execute("SELECT key, value FROM header").fetchall())
    except sqlite3.DatabaseError:
        raise CacheError(f"{name or 'the file'} is not a cache exported by `atlas cache export`") from None
    fmt = rows.get("format", "")
    if fmt != FORMAT:
        newer = fmt.startswith("atlas-cache-")
        raise CacheError(f"{name or 'the file'} " + (f"has the format {fmt}, of a newer atlas (this one reads {FORMAT})"
                                                     if newer else "is not a cache exported by `atlas cache export`"))
    rows["counts"] = json.loads(rows.get("counts") or "{}")
    return rows


def pack(cfg: Config, path: str | Path, with_judgements: bool = False) -> dict:
    """Writes to `path` (a new file, which replaces it at the end) the vectors of the
    embeddings model of the configuration, the analyses of sections and, with
    `with_judgements`, the caches of judgements of verify, comply and link. Returns the
    header, plus `unattributed`: cached vectors whose model is not known (cached by
    atlas 0.2 for texts that are no longer in the index), which are not exported."""
    from atlas import __version__
    from atlas.index.embeddings import document_key, prefixes
    from atlas.llm import from_config
    from atlas.index.store import IndexStore
    ws, path = cfg.workspace, Path(path)
    if not (ws / "atlas.db").exists():
        raise CacheError(f"nothing to export: {ws} has no atlas.db (run `atlas run` first)")
    chat, emb = _models(cfg)
    with _lock(cfg):
        store = IndexStore(ws / "atlas.db")
        if store.cache_without_model():          # atlas 0.2 did not record the model of a vector
            d_prefix = prefixes(from_config(cfg).embeddings)[1]
            store.attribute_cache(emb, [document_key(emb, d_prefix, t, title) for t, title in store.embed_texts()])
        vectors = store.cache_rows(emb)
        unattributed = store.cache_without_model()
        store.db.close()
        files = []
        for f in sorted(docs_cache(cfg).glob("*.json")) if docs_cache(cfg).is_dir() else []:
            try:
                files.append(("docs", f.name, f.read_text(encoding="utf-8")))
            except OSError as e:
                log.warning("%s not exported: %s", f, e)
        judgements = 0
        if with_judgements:
            for name in JUDGEMENT_FILES:
                f = ws / "reports" / name
                if f.exists():
                    content = f.read_text(encoding="utf-8")
                    judgements += len(json.loads(content or "{}"))
                    files.append(("judgements", name, content))

    counts = {"vectors": len(vectors), "analyses": sum(k == "docs" for k, _, _ in files), "judgements": judgements}
    head = {"format": FORMAT, "atlas_version": __version__, "project": cfg["project"]["name"], "chat_model": chat,
            "embeddings_model": emb, "created": datetime.now().isoformat(timespec="seconds"),
            "counts": json.dumps(counts)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    try:
        with db:
            db.executescript(SCHEMA)
            db.executemany("INSERT INTO header VALUES (?, ?)", head.items())
            db.executemany("INSERT INTO embeddings VALUES (?, ?, ?)", [(k, v, emb) for k, v in vectors])
            db.executemany("INSERT INTO files VALUES (?, ?, ?)", files)
    finally:
        db.close()
    os.replace(tmp, path)
    log.info("exported %s to %s", counts, path)
    return {**head, "counts": counts, "unattributed": unattributed}


def unpack(cfg: Config, path: str | Path) -> dict:
    """Adds to the workspace what `path` (made by pack) has and the workspace lacks:
    vectors, analyses of sections and judgements. Nothing is ever replaced. CacheError if
    the file is not an exported cache or its vectors are of another embeddings model.
    Returns the header of the file and, for each part, how many entries went in (`new`)
    and how many were already there (`present`); `warnings`, what does not fit."""
    from atlas.index.store import IndexStore
    from atlas.pipeline import atomic_write
    path = Path(path)
    if not path.is_file():
        raise CacheError(f"{path} does not exist")
    chat, emb = _models(cfg)
    src = read_only(path)
    try:
        head = header(src, path.name)
        if head["counts"].get("vectors") and head["embeddings_model"] != emb:
            raise CacheError(
                f"{path.name} has vectors of the embeddings model '{head['embeddings_model']}' and this "
                f"configuration uses '{emb}': the key of a vector includes its model, so none of them would "
                f"ever be used. Export it again from a workspace with [embeddings] model = \"{emb}\"")
        warnings = []
        if head["counts"].get("analyses") and head["chat_model"] != chat:
            warnings.append(f"the analyses of {path.name} are of the chat model '{head['chat_model']}' and this "
                            f"configuration uses '{chat}': they are only used with that model")
        out: dict[str, Any] = {"header": head, "vectors": {"new": 0, "present": 0},
                               "analyses": {"new": 0, "present": 0}, "judgements": {}, "warnings": warnings}
        with _lock(cfg):
            store = IndexStore(cfg.workspace / "atlas.db")
            try:
                rows = src.execute("SELECT key, vector, model FROM embeddings WHERE model = ?", (emb,))
                while batch := rows.fetchmany(5000):
                    good = [r for r in batch if _KEY.match(r[0] or "") and r[1] and len(r[1]) % 4 == 0]
                    new = store.cache_add(good)
                    out["vectors"]["new"] += new
                    out["vectors"]["present"] += len(good) - new
            finally:
                store.db.close()

            folder = docs_cache(cfg)
            for name, content in src.execute("SELECT name, content FROM files WHERE kind = 'docs' ORDER BY name"):
                if not _NAME.match(name or ""):
                    continue                                         # never a path out of the folder
                if (folder / name).exists():
                    out["analyses"]["present"] += 1
                    continue
                try:
                    if not isinstance(json.loads(content), dict):
                        continue
                except (TypeError, ValueError):
                    continue
                atomic_write(folder / name, content)
                out["analyses"]["new"] += 1

            for name, content in src.execute("SELECT name, content FROM files WHERE kind = 'judgements'"):
                if name not in JUDGEMENT_FILES:
                    continue
                try:
                    theirs = json.loads(content or "{}")
                except ValueError:
                    continue
                target = cfg.workspace / "reports" / name
                try:
                    ours = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
                except ValueError:
                    ours = {}
                added = {k: v for k, v in theirs.items() if k not in ours}
                if added:
                    atomic_write(target, json.dumps({**ours, **added}, ensure_ascii=False, indent=1))
                out["judgements"][name] = {"new": len(added), "present": len(theirs) - len(added)}
    finally:
        src.close()
    log.info("imported from %s: %s", path, {k: out[k] for k in ("vectors", "analyses", "judgements")})
    return out


# --------------------------------------------------------------------------
# Shared cache: read only, in a folder of exported caches
# --------------------------------------------------------------------------

class SharedCache:
    """The exported caches (*.sqlite) of a folder, read only: vectors of the embeddings
    model `embeddings_model` and analyses of sections. The newest file is asked first.
    The folder is read on first use; if it, or a file, cannot be read, it is left out
    with one warning and atlas goes on without it. Thread-safe."""

    def __init__(self, folder: str | Path, embeddings_model: str):
        self.folder = Path(folder)
        self.model = embeddings_model
        self.hits = {"vectors": 0, "analyses": 0}
        self._files: list[dict] | None = None
        self._lock = threading.RLock()
        self._warned: set[str] = set()

    def _warn(self, key: str, message: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    def _open(self) -> list[dict]:
        with self._lock:
            if self._files is not None:
                return self._files
            self._files = []
            try:
                found = sorted(self.folder.glob("*.sqlite"), key=lambda f: f.stat().st_mtime, reverse=True)
                if not self.folder.is_dir():
                    raise FileNotFoundError("not a folder")
            except OSError as e:
                self._warn("folder", "shared cache %s is not accessible (%s): going on without it", self.folder, e)
                return self._files
            for f in found:
                try:
                    db = read_only(f)
                    head = header(db, f.name)
                except (CacheError, sqlite3.Error, OSError) as e:
                    self._warn(str(f), "shared cache %s left out: %s", f, e)
                    continue
                vectors = head["embeddings_model"] == self.model
                if not vectors and head["counts"].get("vectors"):
                    self._warn(f"model:{f}", "shared cache %s has vectors of '%s', not of '%s': only its analyses "
                               "are used", f, head["embeddings_model"], self.model)
                self._files.append({"file": f, "db": db, "vectors": vectors})
            if not self._files:
                self._warn("empty", "shared cache %s has no exported cache (*.sqlite): going on without it",
                           self.folder)
            else:
                log.info("shared cache: %s", ", ".join(x["file"].name for x in self._files))
            return self._files

    def _query(self, x: dict, sql: str, params: list) -> list:
        """A query to one file; a file that stops being readable (the network) is left out."""
        try:
            return x["db"].execute(sql, params).fetchall()
        except sqlite3.Error as e:
            self._warn(f"read:{x['file']}", "shared cache %s cannot be read any more (%s): left out", x["file"], e)
            self._files = [y for y in self._files or [] if y is not x]
            return []

    def vectors(self, keys: list[str]) -> dict[str, tuple[bytes, str]]:
        """{key: (vector, model)} of the keys that some file has."""
        out: dict[str, tuple[bytes, str]] = {}
        with self._lock:
            for x in list(self._open()):
                missing = [k for k in keys if k not in out]
                if not missing:
                    break
                if not x["vectors"]:
                    continue
                for i in range(0, len(missing), 500):
                    part = missing[i:i + 500]
                    for key, blob in self._query(x, f"SELECT key, vector FROM embeddings WHERE model = ? AND key IN "
                                                    f"({','.join('?' * len(part))})", [self.model, *part]):
                        if blob and len(blob) % 4 == 0:
                            out[key] = (blob, self.model)
            self.hits["vectors"] += len(out)
        return out

    def analysis(self, name: str) -> str | None:
        """The content of the analysis `name` (a file of docs/.cache), or None."""
        with self._lock:
            for x in list(self._open()):
                rows = self._query(x, "SELECT content FROM files WHERE kind = 'docs' AND name = ?", [name])
                if rows:
                    self.hits["analyses"] += 1
                    return rows[0][0]
        return None

    def has_analysis(self, name: str) -> bool:
        with self._lock:
            return any(self._query(x, "SELECT 1 FROM files WHERE kind = 'docs' AND name = ?", [name])
                       for x in list(self._open()))

    def close(self) -> None:
        with self._lock:
            for x in self._files or []:
                x["db"].close()
            self._files = None


_SHARED: dict[tuple[str, str], SharedCache] = {}
_SHARED_LOCK = threading.Lock()


def shared_folder(cfg: Config) -> Path | None:
    """[project] shared_cache, relative to the folder of atlas.toml; None if it is empty."""
    value = (cfg["project"].get("shared_cache") or "").strip()
    if not value:
        return None
    p = Path(value).expanduser()
    return p if p.is_absolute() or value.startswith(("//", "\\\\")) else cfg.path.parent / p


def shared_cache(cfg: Config) -> SharedCache | None:
    """The shared cache of [project] shared_cache (one per folder and model in this
    process, so a folder that cannot be read is only warned about once), or None."""
    folder = shared_folder(cfg)
    if folder is None:
        return None
    _, emb = _models(cfg)
    with _SHARED_LOCK:
        key = (str(folder), emb)
        if key not in _SHARED:
            _SHARED[key] = SharedCache(folder, emb)
        return _SHARED[key]
