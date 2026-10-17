"""The connection to the workspace database (atlas.db, and reference.db), and its schema
version.

Every class that keeps tables there (Store, Ledger, IndexStore, GraphStore) opens it with
`connect(path)`: the same pragmas for all of them, and the schema brought up to date
before anyone reads it.

The version is SQLite's `PRAGMA user_version`. A database without tables is new: it is
stamped with SCHEMA_VERSION and each class creates its current tables. One with tables and
an older version is migrated: a copy first (`atlas.db.bak-<old version>`, next to it), then
MIGRATIONS[old + 1] ... MIGRATIONS[SCHEMA_VERSION] and the new version in a single
transaction, so a migration that fails leaves the database as it was. One with a newer
version is refused: this atlas does not know its tables.

Rules for a migration (when a table changes, SCHEMA_VERSION goes up by one and its
function goes in MIGRATIONS):
- It receives the connection inside the transaction: `execute`, never `executescript`
  (which commits first).
- A table may not exist (a workspace that never ran a step; reference.db only has the
  index tables): look in sqlite_master before touching it.
- FTS5 tables are not altered: drop them and create them again, filled from the table
  they index (`atlas.index.store.recreate_fts`).
- The data that a step derives from its sources needs no migration: change the step's
  version (DOCS_VERSION, INDEX_VERSION...) and the next run reprocesses it.

    db = connect(workspace / "atlas.db")
    schema_version(db)                      # SCHEMA_VERSION once open
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Callable

from atlas.config import ConfigError

__all__ = ["SCHEMA_VERSION", "MIGRATIONS", "connect", "schema_version"]

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2


def _tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _ledger_seconds(db: sqlite3.Connection) -> None:
    """1: the `seconds` of each unit in the ledger (ledgers before 0.1.0)."""
    if "ledger" in _tables(db) and "seconds" not in {r[1] for r in db.execute("PRAGMA table_info(ledger)")}:
        db.execute("ALTER TABLE ledger ADD COLUMN seconds REAL")


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _meta_and_model(db: sqlite3.Connection) -> None:
    """2: the `meta` of each record of the index (what a search can be narrowed by) and the
    `model` of each cached vector (to export it to other workstations), of atlas 0.3."""
    tables = _tables(db)
    if "index_records" in tables and "meta" not in _columns(db, "index_records"):
        db.execute("ALTER TABLE index_records ADD COLUMN meta TEXT")
    if "embed_cache" in tables and "model" not in _columns(db, "embed_cache"):
        db.execute("ALTER TABLE embed_cache ADD COLUMN model TEXT")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _ledger_seconds, 2: _meta_and_model}


def schema_version(db: sqlite3.Connection) -> int:
    return db.execute("PRAGMA user_version").fetchone()[0]


def connect(path: str | Path) -> sqlite3.Connection:
    """A connection to the database at `path` (created if missing), shared by threads (the
    caller serialises them), with sqlite3.Row rows and the schema at SCHEMA_VERSION."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    try:
        _migrate(db, path)
    except BaseException:
        db.close()
        raise
    return db


def _migrate(db: sqlite3.Connection, path: Path) -> None:
    if schema_version(db) == SCHEMA_VERSION:
        return
    db.execute("BEGIN IMMEDIATE")       # another process opening it at the same time waits here
    try:
        old = schema_version(db)        # again, under the lock: it may have migrated it already
        if old > SCHEMA_VERSION:
            raise ConfigError("this workspace is from a newer atlas: update atlas or use another workspace "
                              f"({path} has schema version {old}; this atlas knows up to {SCHEMA_VERSION})")
        backup = path.with_name(f"{path.name}.bak-{old}") if old < SCHEMA_VERSION and _tables(db) else None
        if backup:
            _copy(path, backup)
            for n in range(old + 1, SCHEMA_VERSION + 1):
                MIGRATIONS[n](db)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    if backup:
        log.info("%s: schema migrated from version %d to %d; the previous one is in %s",
                 path, old, SCHEMA_VERSION, backup)


def _copy(path: Path, target: Path) -> None:
    """A consistent copy of the database, taken while the caller holds the write lock: by
    another connection (SQLite's backup from the connection that holds the lock never
    ends), which can still read because nothing has been written yet."""
    source, copy = sqlite3.connect(path, timeout=30), sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
