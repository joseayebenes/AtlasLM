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
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from atlas.config import Config

__all__ = ["CacheError", "FORMAT", "JUDGEMENT_FILES", "docs_cache", "header", "pack", "read_only", "unpack"]

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
    from atlas.index.embeddings import document_key
    from atlas.index.store import IndexStore
    ws, path = cfg.workspace, Path(path)
    if not (ws / "atlas.db").exists():
        raise CacheError(f"nothing to export: {ws} has no atlas.db (run `atlas run` first)")
    chat, emb = _models(cfg)
    with _lock(cfg):
        store = IndexStore(ws / "atlas.db")
        if store.cache_without_model():          # atlas 0.2 did not record the model of a vector
            store.attribute_cache(emb, [document_key(emb, t) for t in store.embed_texts()])
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
        out = {"header": head, "vectors": {"new": 0, "present": 0}, "analyses": {"new": 0, "present": 0},
               "judgements": {}, "warnings": warnings}
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
                new = {k: v for k, v in theirs.items() if k not in ours}
                if new:
                    atomic_write(target, json.dumps({**ours, **new}, ensure_ascii=False, indent=1))
                out["judgements"][name] = {"new": len(new), "present": len(theirs) - len(new)}
    finally:
        src.close()
    log.info("imported from %s: %s", path, {k: out[k] for k in ("vectors", "analyses", "judgements")})
    return out
