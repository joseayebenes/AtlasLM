"""Workspace store: a single atlas.db with the data of every source (the engine's ledger
lives in the same file, in the `ledger` table)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS requirements (
    id TEXT PRIMARY KEY, module TEXT, text TEXT, parent TEXT, attributes TEXT, hash TEXT, raw_id TEXT);
CREATE INDEX IF NOT EXISTS idx_req_module ON requirements(module);
CREATE TABLE IF NOT EXISTS links (
    source TEXT, target TEXT, type TEXT, module TEXT, PRIMARY KEY (source, target, type));
CREATE TABLE IF NOT EXISTS code_units (
    file TEXT PRIMARY KEY, unit TEXT, language TEXT, data TEXT);
"""


class Store:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path = self.workspace / "atlas.db"
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)

    # --- requirements -------------------------------------------------------
    def replace_module(self, module: str, reqs: list, links: list, hashes: dict[str, str]) -> None:
        with self.db:                                   # transaction: all or nothing
            self.db.execute("DELETE FROM requirements WHERE module=?", (module,))
            self.db.execute("DELETE FROM links WHERE module=?", (module,))
            self.db.executemany(
                "INSERT OR REPLACE INTO requirements VALUES (?,?,?,?,?,?,?)",
                [(r.id, r.module, r.text, r.parent, json.dumps(r.attributes, ensure_ascii=False, default=str),
                  hashes[r.id], r.raw_id) for r in reqs])
            self.db.executemany("INSERT OR REPLACE INTO links VALUES (?,?,?,?)",
                                [(l.source, l.target, l.type, module) for l in links])

    def delete_module(self, module: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM requirements WHERE module=?", (module,))
            self.db.execute("DELETE FROM links WHERE module=?", (module,))

    def requirement_hashes(self, module: str) -> dict[str, str]:
        return {r["id"]: r["hash"] for r in self.db.execute("SELECT id, hash FROM requirements WHERE module=?", (module,))}

    def requirement(self, rid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM requirements WHERE id=?", (rid.upper(),)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["attributes"] = json.loads(d["attributes"] or "{}")
        d["links_out"] = [dict(x) for x in self.db.execute("SELECT target, type FROM links WHERE source=?", (d["id"],))]
        d["links_in"] = [dict(x) for x in self.db.execute("SELECT source, type FROM links WHERE target=?", (d["id"],))]
        return d

    # --- code ---------------------------------------------------------------
    def put_code_unit(self, unit) -> None:
        from dataclasses import asdict
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO code_units VALUES (?,?,?,?)",
                            (unit.file, unit.unit, unit.language, json.dumps(asdict(unit), ensure_ascii=False)))

    def delete_code_unit(self, file: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM code_units WHERE file=?", (file,))

    def counts(self) -> dict[str, int]:
        return {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("requirements", "links", "code_units")}
