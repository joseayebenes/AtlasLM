"""The requirements exchange database: the one way requirements reach atlas.

atlas always reads requirements from a SQLite database with this fixed schema; what
changes from one project to another is who fills it. A filler (the DOORS export of
tools/doors_export, a ReqIF or spreadsheet import...) maps its source to these columns,
and atlas never needs to know where the data came from.

    exchange_meta           schema_version, filler, filled_at
    exchange_modules        module, source, filled_at, snapshot (changes when the module does)
    exchange_requirements   module, id, text, heading, kind, parent, position, level, outline,
                            attributes (JSON), source_key
    exchange_links          source, target, type, module, target_module

Rules of the contract, checked by ExchangeWriter:
- `id` is the requirement ID as it is written in documents and code, unique in the
  database. `kind` is requirement | heading | information.
- `parent` is the ID of the parent object (the hierarchy), or NULL at the top.
- `position` is the order of the object in its module; `level` its depth (1 = top).
- `attributes` is a JSON object {name: value}; names are the filler's choice, except
  kind, heading, outline and level, which atlas adds from the columns.
- A link goes from a requirement of the module (`source`) to any requirement (`target`,
  maybe of another module, `target_module`); `type` is satisfies, verifies, refines...
- A module is replaced whole, in one transaction, together with its snapshot.

This module uses only the standard library, so that a filler can use it, or copy it,
without the rest of atlas.

    with ExchangeWriter("requirements.sqlite", filler="my-import") as w:
        w.replace_module("SRS_TMS", requirements=[{"id": "SRS-0001", "text": "..."}],
                         links=[{"source": "SRS-0001", "target": "SSS-0003", "type": "satisfies"}])
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

__all__ = ["KINDS", "RESERVED_ATTRIBUTES", "SCHEMA_VERSION", "ExchangeError", "ExchangeWriter", "open_exchange"]

SCHEMA_VERSION = 1
KINDS = ("requirement", "heading", "information")
# atlas adds these to the attributes of every requirement it reads: a filler cannot use them
RESERVED_ATTRIBUTES = ("kind", "heading", "outline", "level")

SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exchange_modules (
    module     TEXT PRIMARY KEY,
    source     TEXT NOT NULL DEFAULT '',
    filled_at  TEXT NOT NULL,
    snapshot   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_requirements (
    module      TEXT NOT NULL REFERENCES exchange_modules(module) ON DELETE CASCADE,
    id          TEXT NOT NULL PRIMARY KEY,
    text        TEXT NOT NULL DEFAULT '',
    heading     TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'requirement',
    parent      TEXT,
    position    INTEGER NOT NULL,
    level       INTEGER NOT NULL DEFAULT 1,
    outline     TEXT NOT NULL DEFAULT '',
    attributes  TEXT NOT NULL DEFAULT '{}',
    source_key  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_exchange_requirements_module ON exchange_requirements (module, position);
CREATE TABLE IF NOT EXISTS exchange_links (
    source         TEXT NOT NULL,
    target         TEXT NOT NULL,
    type           TEXT NOT NULL,
    module         TEXT NOT NULL REFERENCES exchange_modules(module) ON DELETE CASCADE,
    target_module  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source, target, type)
);
CREATE INDEX IF NOT EXISTS idx_exchange_links_module ON exchange_links (module);
"""


class ExchangeError(ValueError):
    """Data that breaks the contract, or a database that is not an exchange database."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_exchange(path: str | Path, read_only: bool = True) -> sqlite3.Connection:
    """A connection to an exchange database whose schema version this atlas reads."""
    path = Path(path).expanduser()
    if read_only:
        if not path.exists():
            raise FileNotFoundError(f"exchange database {path} does not exist")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if read_only:
        try:
            row = conn.execute("SELECT value FROM exchange_meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError:
            conn.close()
            raise ExchangeError(f"{path} is not a requirements exchange database") from None
        if row is None or int(row[0]) != SCHEMA_VERSION:
            conn.close()
            raise ExchangeError(f"{path} has exchange schema version {row[0] if row else '?'}; "
                                f"this atlas reads version {SCHEMA_VERSION}")
    return conn


class ExchangeWriter:
    """Fills an exchange database, module by module, checking the contract."""

    def __init__(self, path: str | Path, filler: str):
        self.path = Path(path)
        self.conn = open_exchange(self.path, read_only=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        with self.conn:
            found = self.conn.execute("SELECT value FROM exchange_meta WHERE key = 'schema_version'").fetchone()
            if found and int(found[0]) != SCHEMA_VERSION:
                raise ExchangeError(f"{self.path} has exchange schema version {found[0]}, not {SCHEMA_VERSION}")
            self.conn.executemany("INSERT OR REPLACE INTO exchange_meta VALUES (?, ?)",
                                  [("schema_version", str(SCHEMA_VERSION)), ("filler", filler), ("filled_at", _now())])

    def __enter__(self) -> "ExchangeWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _check(module: str, requirements: list[dict], links: list[dict]) -> list[str]:
        """Raises ExchangeError for what breaks the contract; returns warnings."""
        if not module.strip():
            raise ExchangeError("the module name is empty")
        ids: set[str] = set()
        for i, r in enumerate(requirements):
            rid = str(r.get("id") or "").strip()
            if not rid:
                raise ExchangeError(f"{module}: requirement #{i + 1} has no id")
            if rid in ids:
                raise ExchangeError(f"{module}: the id {rid} is repeated")
            ids.add(rid)
            if r.get("kind", "requirement") not in KINDS:
                raise ExchangeError(f"{module}: {rid} has kind {r.get('kind')!r} (valid: {', '.join(KINDS)})")
            attributes = r.get("attributes", {})
            if not isinstance(attributes, dict):
                raise ExchangeError(f"{module}: the attributes of {rid} are not a dict")
            reserved = sorted(set(attributes) & set(RESERVED_ATTRIBUTES))
            if reserved:
                raise ExchangeError(f"{module}: {rid} uses reserved attribute names {reserved}")
        warnings = [f"{module}: the parent {r['parent']} of {r['id']} is not in the module"
                    for r in requirements if r.get("parent") and r["parent"] not in ids]
        for l in links:
            if not (l.get("source") and l.get("target") and l.get("type")):
                raise ExchangeError(f"{module}: link without source, target or type: {l}")
            if l["source"] not in ids:
                raise ExchangeError(f"{module}: the link {l['source']} -> {l['target']} starts outside the module")
        return warnings

    def replace_module(self, module: str, requirements: Iterable[dict], links: Iterable[dict] = (),
                       source: str = "") -> list[str]:
        """Replaces the content of a module in one transaction. Returns the warnings
        (e.g. a parent outside the module); raises ExchangeError if the data breaks the
        contract, and then nothing is written."""
        requirements, links = list(requirements), list(links)
        warnings = self._check(module, requirements, links)
        rows = [(module, str(r["id"]).strip(), r.get("text", ""), r.get("heading", ""), r.get("kind", "requirement"),
                 r.get("parent") or None, int(r.get("position", i)), int(r.get("level", 1)), r.get("outline", ""),
                 json.dumps(r.get("attributes", {}), ensure_ascii=False, sort_keys=True), str(r.get("source_key", "")))
                for i, r in enumerate(requirements, 1)]
        link_rows = sorted({(l["source"], l["target"], l["type"], module, l.get("target_module", "")) for l in links})
        snapshot = hashlib.sha256(json.dumps([rows, link_rows], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        with self.conn:
            self.conn.execute("DELETE FROM exchange_links WHERE module = ?", (module,))
            self.conn.execute("DELETE FROM exchange_requirements WHERE module = ?", (module,))
            self.conn.execute("INSERT OR REPLACE INTO exchange_modules VALUES (?, ?, ?, ?)",
                              (module, source, _now(), snapshot))
            try:
                self.conn.executemany("INSERT INTO exchange_requirements VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            except sqlite3.IntegrityError as e:          # an id already used by another module
                raise ExchangeError(f"{module}: {e} (requirement ids must be unique in the database)") from None
            self.conn.executemany("INSERT INTO exchange_links VALUES (?,?,?,?,?)", link_rows)
        return warnings

    def remove_module(self, module: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM exchange_links WHERE module = ?", (module,))
            self.conn.execute("DELETE FROM exchange_requirements WHERE module = ?", (module,))
            self.conn.execute("DELETE FROM exchange_modules WHERE module = ?", (module,))

    def modules(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT module FROM exchange_modules ORDER BY module")]
