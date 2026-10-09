"""Raw copy of DOORS in SQLite: what doors_download.py reads, as DOORS gives it.

First of the two files of the download (the second, doors_download.py, says how it is read
from DOORS). Neither imports atlas: they can be copied alone to the Windows machine with
DOORS. publish.py turns this raw copy into the requirements exchange database of atlas,
applying the mapping of mapping.toml; that step does not need DOORS.

Comes from scripts/modelo_sqlite.py of the doors_kb project, with the hierarchy (parent and
level of every object), the outgoing links, the attribute definitions of every module and a
schema version that upgrades databases of the previous version.

Decisions worth keeping in mind:
- The identity of an object is (module_path, absolute_number): the Absolute Number alone is
  repeated across modules.
- Every attribute is kept by its DOORS name in its own table: a new attribute in DOORS does
  not need a schema change.
- Deletions are logical (is_deleted): an object that disappears from DOORS may come back.
- Every object keeps a SHA-256 of its content, parent, level and outgoing links included:
  it is what makes the second download and the next ones incremental, and a new link or a
  moved object counts as a change.

DOORS is always the source of truth: this database can be rebuilt and nothing is ever
written back. Run directly, it prints a summary:

    python tools/doors_export/raw_store.py doors_raw.sqlite
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

SCHEMA_VERSION = 2       # 1 = scripts/modelo_sqlite.py of doors_kb (no hierarchy, no links)

# DOORS attributes also copied to columns of `objects`, so that SQL by hand is readable
HEADING = "Object Heading"
TEXT = "Object Text"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Downloaded modules and how fresh they are.
CREATE TABLE IF NOT EXISTS modules (
    module_path        TEXT PRIMARY KEY,
    last_sync_at       TEXT,
    last_full_sync_at  TEXT
);

-- Object attributes declared by every module (name, DOORS type, system attribute or not).
CREATE TABLE IF NOT EXISTS attribute_definitions (
    module_path  TEXT NOT NULL REFERENCES modules(module_path) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL DEFAULT '',
    is_system    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (module_path, name)
) WITHOUT ROWID;

-- Main data of every DOORS object and the hash of its content. (The table keeps the name
-- `requirements` of version 1 so that old databases upgrade in place.)
CREATE TABLE IF NOT EXISTS requirements (
    id                      INTEGER PRIMARY KEY,
    module_path             TEXT    NOT NULL REFERENCES modules(module_path) ON DELETE CASCADE,
    absolute_number         INTEGER NOT NULL,
    identifier              TEXT    NOT NULL DEFAULT '',
    outline_number          TEXT    NOT NULL DEFAULT '',
    heading                 TEXT    NOT NULL DEFAULT '',
    text                    TEXT    NOT NULL DEFAULT '',
    parent_absolute_number  INTEGER,
    level                   INTEGER NOT NULL DEFAULT 0,
    position                INTEGER NOT NULL DEFAULT 0,
    content_hash            TEXT    NOT NULL,
    is_deleted              INTEGER NOT NULL DEFAULT 0,
    first_seen_at           TEXT    NOT NULL,
    last_seen_at            TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL,
    deleted_at              TEXT,
    UNIQUE (module_path, absolute_number)
);
CREATE INDEX IF NOT EXISTS idx_requirements_active ON requirements (module_path, is_deleted);

-- Any attribute of the project, without one SQL column per attribute.
CREATE TABLE IF NOT EXISTS requirement_attributes (
    requirement_id  INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    value_text      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (requirement_id, name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_attributes_name ON requirement_attributes (name);

-- Outgoing links of every object: target module and Absolute Number, and the link module.
CREATE TABLE IF NOT EXISTS links (
    requirement_id          INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    target_module           TEXT    NOT NULL,
    target_absolute_number  INTEGER NOT NULL,
    link_module             TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (requirement_id, target_module, target_absolute_number, link_module)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_links_target ON links (target_module, target_absolute_number);

-- Every download: what explains why the copy is as it is, and tells a complete download
-- from one that stopped halfway.
CREATE TABLE IF NOT EXISTS sync_runs (
    id                 INTEGER PRIMARY KEY,
    module_path        TEXT    NOT NULL,
    started_at         TEXT    NOT NULL,
    finished_at        TEXT,
    status             TEXT    NOT NULL,  -- running | success | failed
    pages              INTEGER NOT NULL DEFAULT 0,
    requirements_seen  INTEGER NOT NULL DEFAULT 0,
    inserted           INTEGER NOT NULL DEFAULT 0,
    updated            INTEGER NOT NULL DEFAULT 0,
    unchanged          INTEGER NOT NULL DEFAULT 0,
    deleted            INTEGER NOT NULL DEFAULT 0,
    completed_module   INTEGER NOT NULL DEFAULT 0,
    error              TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_runs_module ON sync_runs (module_path, started_at DESC);
"""

# Columns added by version 2 to a version 1 database
_UPGRADE_COLUMNS = {"parent_absolute_number": "INTEGER", "level": "INTEGER NOT NULL DEFAULT 0",
                    "position": "INTEGER NOT NULL DEFAULT 0"}


def now() -> str:
    """UTC timestamp in ISO-8601, which is how every date is kept."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(text: str) -> str:
    """Line breaks before a text goes into the hash: DOORS gives CRLF and, depending on the
    client version, loose CRs. Without this, the same object would be "updated" on every
    download and the incremental work would be lost."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class Link:
    """An outgoing link: target object (module and Absolute Number) and link module."""
    target_module: str
    target_absolute_number: int
    link_module: str = ""


@dataclass(frozen=True)
class DoorsObject:
    """A DOORS object as it is downloaded. `attributes` has every attribute read from the
    module; `heading` and `text` are shortcuts to two of them."""
    module_path: str
    absolute_number: int
    identifier: str = ""
    outline_number: str = ""
    parent_absolute_number: int | None = None
    level: int = 0
    attributes: dict[str, str] = field(default_factory=dict)
    links: tuple[Link, ...] = ()

    @property
    def heading(self) -> str:
        return self.attributes.get(HEADING, "")

    @property
    def text(self) -> str:
        return self.attributes.get(TEXT, "")

    def canonical_payload(self) -> str:
        """The fields that make the content, in canonical form. Deliberately without
        module_path and absolute_number (the identity, not the content); sort_keys and the
        sorted links make the hash independent of the order DXL gives them in."""
        return json.dumps({
            "identifier": _normalize(self.identifier),
            "outline_number": _normalize(self.outline_number),
            "parent": self.parent_absolute_number,
            "level": self.level,
            "attributes": {k: _normalize(v) for k, v in self.attributes.items()},
            "links": sorted([l.target_module, l.target_absolute_number, l.link_module] for l in self.links),
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_payload().encode("utf-8")).hexdigest()


@dataclass
class Stats:
    """Counters of a download, which end up in sync_runs."""
    module_path: str = ""
    pages: int = 0
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    links: int = 0
    completed_module: bool = False
    error: str | None = None

    def count(self, change: str) -> None:
        self.seen += 1
        setattr(self, change, getattr(self, change) + 1)

    def summary(self) -> str:
        state = "complete" if self.completed_module else "PARTIAL"
        return (f"{self.module_path}: {self.seen} objects ({state}), {self.inserted} new, {self.updated} changed, "
                f"{self.unchanged} unchanged, {self.deleted} marked as deleted, {self.links} outgoing links")


class RawStore:
    """The raw copy in SQLite; the only class that writes in it.

        with RawStore("doors_raw.sqlite") as store:
            store.save(obj)
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # WAL lets the database be read (e.g. to follow the progress) while the download writes
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._upgrade()
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.conn.commit()

    def _upgrade(self) -> None:
        """A version 1 database (doors_kb) gets the new columns; its tables stay."""
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(requirements)")}
        if not columns:
            return
        for name, kind in _UPGRADE_COLUMNS.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE requirements ADD COLUMN {name} {kind}")

    def __enter__(self) -> RawStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """The writes of one page in one transaction: if the download stops, what was
        committed is consistent and nothing has to start from scratch."""
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    # --- writing ----------------------------------------------------------------------
    def register_module(self, module_path: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO modules (module_path) VALUES (?)", (module_path,))

    def save_attribute_definitions(self, module_path: str, definitions: Iterable[tuple[str, str, bool]]) -> None:
        """(name, type, is_system) of every object attribute of the module."""
        self.conn.execute("DELETE FROM attribute_definitions WHERE module_path = ?", (module_path,))
        self.conn.executemany("INSERT OR REPLACE INTO attribute_definitions VALUES (?, ?, ?, ?)",
                              [(module_path, n, t, int(s)) for n, t, s in definitions])

    def save(self, obj: DoorsObject, position: int = 0) -> str:
        """Inserts or updates an object; returns 'inserted', 'updated' or 'unchanged'. An
        object that comes back after being marked as deleted is 'updated'."""
        row = self.conn.execute("SELECT id, content_hash, is_deleted, position FROM requirements "
                                "WHERE module_path = ? AND absolute_number = ?",
                                (obj.module_path, obj.absolute_number)).fetchone()
        new_hash, moment = obj.content_hash(), now()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO requirements (module_path, absolute_number, identifier, outline_number, heading, text, "
                "parent_absolute_number, level, position, content_hash, is_deleted, first_seen_at, last_seen_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (obj.module_path, obj.absolute_number, obj.identifier, obj.outline_number, obj.heading, obj.text,
                 obj.parent_absolute_number, obj.level, position, new_hash, moment, moment, moment))
            self._save_details(int(cur.lastrowid or 0), obj)
            return "inserted"
        if row["content_hash"] == new_hash and not row["is_deleted"]:
            # Unchanged: only seen again (and its place, which is not content). Attributes
            # and links are not touched: that is most of the gain of an incremental download.
            self.conn.execute("UPDATE requirements SET last_seen_at = ?, position = ? WHERE id = ?",
                              (moment, position, row["id"]))
            return "unchanged"
        self.conn.execute(
            "UPDATE requirements SET identifier = ?, outline_number = ?, heading = ?, text = ?, "
            "parent_absolute_number = ?, level = ?, position = ?, content_hash = ?, is_deleted = 0, deleted_at = NULL, "
            "last_seen_at = ?, updated_at = ? WHERE id = ?",
            (obj.identifier, obj.outline_number, obj.heading, obj.text, obj.parent_absolute_number, obj.level,
             position, new_hash, moment, moment, row["id"]))
        self._save_details(int(row["id"]), obj)
        return "updated"

    def _save_details(self, requirement_id: int, obj: DoorsObject) -> None:
        """Rewrites attributes and links: an attribute or link that disappears in DOORS
        also disappears here."""
        self.conn.execute("DELETE FROM requirement_attributes WHERE requirement_id = ?", (requirement_id,))
        self.conn.executemany("INSERT INTO requirement_attributes VALUES (?, ?, ?)",
                              [(requirement_id, n, v) for n, v in obj.attributes.items()])
        self.conn.execute("DELETE FROM links WHERE requirement_id = ?", (requirement_id,))
        self.conn.executemany("INSERT OR IGNORE INTO links VALUES (?, ?, ?, ?)",
                              [(requirement_id, l.target_module, l.target_absolute_number, l.link_module)
                               for l in obj.links])

    def mark_missing(self, module_path: str, seen: Iterable[int]) -> int:
        """Marks as deleted the objects that are no longer in DOORS, and drops their links.

        **Only call it when the whole module was walked.** An interrupted download saw part
        of the module, and marking "what did not appear" would delete living objects.
        The seen numbers go through a temporary table: an `IN (?, ?...)` with one parameter
        per object breaks on large modules."""
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen_numbers (n INTEGER PRIMARY KEY)")
        self.conn.execute("DELETE FROM seen_numbers")
        self.conn.executemany("INSERT OR IGNORE INTO seen_numbers VALUES (?)", [(n,) for n in seen])
        gone = "module_path = ? AND is_deleted = 0 AND absolute_number NOT IN (SELECT n FROM seen_numbers)"
        self.conn.execute(f"DELETE FROM links WHERE requirement_id IN (SELECT id FROM requirements WHERE {gone})",
                          (module_path,))
        moment = now()
        cur = self.conn.execute(f"UPDATE requirements SET is_deleted = 1, deleted_at = ?, updated_at = ? WHERE {gone}",
                                (moment, moment, module_path))
        return cur.rowcount

    def note_freshness(self, module_path: str, *, complete: bool) -> None:
        moment = now()
        if complete:
            self.conn.execute("UPDATE modules SET last_sync_at = ?, last_full_sync_at = ? WHERE module_path = ?",
                              (moment, moment, module_path))
        else:
            self.conn.execute("UPDATE modules SET last_sync_at = ? WHERE module_path = ?", (moment, module_path))

    def start_run(self, module_path: str) -> int:
        cur = self.conn.execute("INSERT INTO sync_runs (module_path, started_at, status) VALUES (?, ?, 'running')",
                                (module_path, now()))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, stats: Stats) -> None:
        """Closes the history of a download, whether it went well or failed."""
        self.conn.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, pages = ?, requirements_seen = ?, inserted = ?, "
            "updated = ?, unchanged = ?, deleted = ?, completed_module = ?, error = ? WHERE id = ?",
            (now(), "failed" if stats.error else "success", stats.pages, stats.seen, stats.inserted, stats.updated,
             stats.unchanged, stats.deleted, 1 if stats.completed_module else 0, stats.error, run_id))
        self.conn.commit()

    # --- reading (for publish.py) -------------------------------------------------------
    def modules(self, complete_only: bool = True) -> list[str]:
        where = "WHERE last_full_sync_at IS NOT NULL" if complete_only else ""
        return [r[0] for r in self.conn.execute(f"SELECT module_path FROM modules {where} ORDER BY module_path")]

    def attribute_names(self, module_path: str) -> set[str]:
        """The object attributes of a module: its definitions, or (for a version 1
        database, which has none) the names found in its objects."""
        names = {r[0] for r in self.conn.execute("SELECT name FROM attribute_definitions WHERE module_path = ?",
                                                 (module_path,))}
        return names or {r[0] for r in self.conn.execute(
            "SELECT DISTINCT a.name FROM requirement_attributes a JOIN requirements r ON r.id = a.requirement_id "
            "WHERE r.module_path = ?", (module_path,))}

    def objects(self, module_path: str) -> list[DoorsObject]:
        """The living objects of a module, in document order, with attributes and links."""
        rows = self.conn.execute("SELECT * FROM requirements WHERE module_path = ? AND is_deleted = 0 "
                                 "ORDER BY position, absolute_number", (module_path,)).fetchall()
        attributes: dict[int, dict[str, str]] = {}
        for r in self.conn.execute("SELECT a.* FROM requirement_attributes a JOIN requirements q "
                                   "ON q.id = a.requirement_id WHERE q.module_path = ?", (module_path,)):
            attributes.setdefault(r["requirement_id"], {})[r["name"]] = r["value_text"]
        links: dict[int, list[Link]] = {}
        for r in self.conn.execute("SELECT l.* FROM links l JOIN requirements q ON q.id = l.requirement_id "
                                   "WHERE q.module_path = ? ORDER BY l.target_module, l.target_absolute_number",
                                   (module_path,)):
            links.setdefault(r["requirement_id"], []).append(
                Link(r["target_module"], r["target_absolute_number"], r["link_module"]))
        return [DoorsObject(module_path=r["module_path"], absolute_number=r["absolute_number"],
                            identifier=r["identifier"], outline_number=r["outline_number"],
                            parent_absolute_number=r["parent_absolute_number"], level=r["level"],
                            attributes=attributes.get(r["id"], {}), links=tuple(links.get(r["id"], [])))
                for r in rows]

    def summary(self) -> list[sqlite3.Row]:
        """One row per module with its counters and freshness."""
        return list(self.conn.execute(
            "SELECT m.module_path, m.last_full_sync_at,"
            "  (SELECT COUNT(*) FROM requirements r WHERE r.module_path = m.module_path AND r.is_deleted = 0) AS alive,"
            "  (SELECT COUNT(*) FROM requirements r WHERE r.module_path = m.module_path AND r.is_deleted = 1) AS deleted,"
            "  (SELECT COUNT(*) FROM links l JOIN requirements r ON r.id = l.requirement_id"
            "   WHERE r.module_path = m.module_path) AS links "
            "FROM modules m ORDER BY m.module_path"))


def _main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Summary of the raw copy of DOORS.")
    parser.add_argument("db", nargs="?", default="doors_raw.sqlite", help="SQLite file")
    args = parser.parse_args()
    with RawStore(args.db) as store:
        rows = store.summary()
        if not rows:
            print(f"{args.db}: no module downloaded yet.")
            return 0
        print(f"{args.db}:")
        for r in rows:
            print(f"  {r['module_path']}: {r['alive']} objects, {r['deleted']} deleted, {r['links']} outgoing links; "
                  f"last complete download: {r['last_full_sync_at'] or 'never'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
