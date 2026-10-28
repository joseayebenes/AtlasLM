"""Workspace store: a single atlas.db with the data of every source (the engine's ledger
lives in the same file, in the `ledger` table). `modules` keeps, for each DOORS module read,
when and from where it was exported and when the run that read it started."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from atlas.db import connect
from atlas.text import plain

__all__ = ["Store"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS requirements (
    id TEXT PRIMARY KEY, module TEXT, text TEXT, parent TEXT, attributes TEXT, hash TEXT, raw_id TEXT);
CREATE INDEX IF NOT EXISTS idx_req_module ON requirements(module);
CREATE TABLE IF NOT EXISTS modules (
    module TEXT PRIMARY KEY, exported_at TEXT, source TEXT, run_started TEXT);
CREATE TABLE IF NOT EXISTS links (
    source TEXT, target TEXT, type TEXT, module TEXT, PRIMARY KEY (source, target, type));
CREATE TABLE IF NOT EXISTS code_units (
    file TEXT PRIMARY KEY, unit TEXT, language TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS documents (
    unit TEXT PRIMARY KEY, source TEXT, path TEXT, out_dir TEXT UNIQUE, title TEXT, data TEXT);
"""


class Store:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path = self.workspace / "atlas.db"
        self.db = connect(self.path)
        self.db.executescript(SCHEMA)
        # Steps write from the Runner's worker threads through this one connection, and the
        # sqlite3 module does not serialise them: every read and transaction takes the lock.
        self.lock = threading.RLock()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    # --- requirements -------------------------------------------------------
    def replace_module(self, module: str, reqs: list, links: list, hashes: dict[str, str]) -> None:
        with self.lock, self.db:                        # transaction: all or nothing
            self.db.execute("DELETE FROM requirements WHERE module=?", (module,))
            self.db.execute("DELETE FROM links WHERE module=?", (module,))
            self.db.executemany(
                "INSERT OR REPLACE INTO requirements VALUES (?,?,?,?,?,?,?)",
                [(r.id, r.module, r.text, r.parent, json.dumps(r.attributes, ensure_ascii=False, default=str),
                  hashes[r.id], r.raw_id) for r in reqs])
            self.db.executemany("INSERT OR REPLACE INTO links VALUES (?,?,?,?)",
                                [(l.source, l.target, l.type, module) for l in links])

    def delete_module(self, module: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM requirements WHERE module=?", (module,))
            self.db.execute("DELETE FROM links WHERE module=?", (module,))
            self.db.execute("DELETE FROM modules WHERE module=?", (module,))

    def put_module_info(self, module: str, exported_at: str, source: str, run_started: str) -> None:
        """When and from where a module was exported (as the adapter says; "" if it does
        not) and when the run that read it started."""
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO modules VALUES (?,?,?,?)",
                            (module, exported_at or "", source or "", run_started))

    def modules_info(self) -> dict[str, dict]:
        """module -> {"exported_at", "source", "run_started"}, for the modules read."""
        return {r["module"]: {k: r[k] or "" for k in ("exported_at", "source", "run_started")}
                for r in self._query("SELECT * FROM modules ORDER BY module")}

    def requirement_hashes(self, module: str) -> dict[str, str]:
        return {r["id"]: r["hash"] for r in self._query("SELECT id, hash FROM requirements WHERE module=?", (module,))}

    def requirement(self, rid: str) -> dict | None:
        with self.lock:
            rows = self._query("SELECT * FROM requirements WHERE id=?", (plain(rid).upper(),))
            if not rows:
                return None
            d = dict(rows[0])
            d["attributes"] = json.loads(d["attributes"] or "{}")
            d["links_out"] = [dict(x) for x in self._query("SELECT target, type FROM links WHERE source=?", (d["id"],))]
            d["links_in"] = [dict(x) for x in self._query("SELECT source, type FROM links WHERE target=?", (d["id"],))]
            return d

    def requirements(self, module: str | None = None, in_module_order: bool = False) -> list[dict]:
        """Every requirement (or those of a module), by module and ID, with its attributes;
        with in_module_order, by module and in the order the adapter read them (that of the
        module in the exchange database)."""
        where, params = ("WHERE module = ?", (module,)) if module else ("", ())
        order = "module, rowid" if in_module_order else "module, id"
        return [{**dict(r), "attributes": json.loads(r["attributes"] or "{}")}
                for r in self._query(f"SELECT * FROM requirements {where} ORDER BY {order}", params)]

    def modules(self) -> list[str]:
        return [r["module"] for r in self._query("SELECT DISTINCT module FROM requirements ORDER BY module")]

    def links(self) -> list[dict]:
        """Every link, in the order they were read (the links of the source before the
        references found in the text)."""
        return [dict(r) for r in self._query("SELECT source, target, type, module FROM links ORDER BY rowid")]

    def requirement_ids(self, contains: str, limit: int = 10) -> list[str]:
        """IDs that contain a text (any case): suggestions for an ID that is not there."""
        return [r["id"] for r in self._query("SELECT id FROM requirements WHERE UPPER(id) LIKE ? ORDER BY id LIMIT ?",
                                             (f"%{plain(contains).strip().upper()}%", limit))]

    # --- code ---------------------------------------------------------------
    def put_code_unit(self, unit) -> None:
        from dataclasses import asdict
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO code_units VALUES (?,?,?,?)",
                            (unit.file, unit.unit, unit.language, json.dumps(asdict(unit), ensure_ascii=False)))

    def code_units(self) -> list[dict]:
        """Every analysed file (CodeUnit as a dict), by file."""
        return [json.loads(r["data"]) for r in self._query("SELECT data FROM code_units ORDER BY file")]

    def delete_code_unit(self, file: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM code_units WHERE file=?", (file,))

    # --- documents ----------------------------------------------------------
    def put_document(self, unit: str, source: str, path: str, out_dir: str, title: str, data: dict) -> None:
        """A processed document: where its output is (structure.json and the Markdown) and
        a summary of it."""
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?)",
                            (unit, source, path, out_dir, title, json.dumps(data, ensure_ascii=False)))

    def document(self, unit: str) -> dict | None:
        rows = self._query("SELECT * FROM documents WHERE unit=?", (unit,))
        return {**dict(rows[0]), "data": json.loads(rows[0]["data"] or "{}")} if rows else None

    def documents(self) -> list[dict]:
        return [{**dict(r), "data": json.loads(r["data"] or "{}")}
                for r in self._query("SELECT * FROM documents ORDER BY unit")]

    def document_owning(self, out_dir: str) -> str | None:
        """The unit whose output is in `out_dir`, if any."""
        rows = self._query("SELECT unit FROM documents WHERE out_dir=?", (out_dir,))
        return rows[0]["unit"] if rows else None

    def delete_document(self, unit: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM documents WHERE unit=?", (unit,))

    def counts(self) -> dict[str, int]:
        return {t: self._query(f"SELECT COUNT(*) FROM {t}")[0][0]
                for t in ("requirements", "links", "code_units", "documents")}
