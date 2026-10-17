import json
import logging
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from atlas.config import ConfigError, load_config
from atlas.db import MIGRATIONS, SCHEMA_VERSION, connect, schema_version
from atlas.pipeline import Ledger
from atlas.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def version(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def test_a_new_workspace_starts_at_the_current_version(tmp_path):
    store = Store(tmp_path / "ws")
    Ledger(store.path)
    assert schema_version(store.db) == SCHEMA_VERSION == max(MIGRATIONS)
    assert [p.name for p in (tmp_path / "ws").iterdir()] == ["atlas.db"]          # nothing to copy


@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURES.glob("atlas-*.db")))
def test_a_workspace_of_a_released_atlas_opens_and_migrates(fixture, toml, tmp_path, capsys, caplog):
    from atlas.cli import status, trace
    db = tmp_path / "ws" / "atlas.db"
    db.parent.mkdir()
    shutil.copy(FIXTURES / fixture, db)
    old = version(db)
    assert old < SCHEMA_VERSION
    cfg = load_config(toml())
    with caplog.at_level(logging.INFO, logger="atlas.db"):
        assert status(cfg) == 0
    out = capsys.readouterr().out
    released = fixture.removeprefix("atlas-").removesuffix(".db")
    assert f"schema: version {SCHEMA_VERSION}; last run with atlas {released}" in out
    assert "'requirements': " in out and "code.parse" in out
    backup = db.with_name(f"atlas.db.bak-{old}")
    assert version(db) == SCHEMA_VERSION and version(backup) == old
    assert f"from version {old} to {SCHEMA_VERSION}" in caplog.text and str(backup) in caplog.text
    with sqlite3.connect(backup) as before, sqlite3.connect(db) as after:
        for table in ("requirements", "ledger", "runs", "graph_nodes", "index_records"):
            count = f"SELECT COUNT(*) FROM {table}"
            assert before.execute(count).fetchone() == after.execute(count).fetchone() != (0,)
    assert trace(cfg, "SRS-0004") == 0
    res = json.loads(capsys.readouterr().out)
    assert res["id"] == "SRS-0004" and res["implemented_by"]
    caplog.clear()
    assert status(cfg) == 0                                       # once migrated, nothing else happens
    assert "migrated" not in caplog.text


def test_a_ledger_before_0_1_0_gains_its_seconds(tmp_path):
    path = tmp_path / "atlas.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ledger (step TEXT NOT NULL, unit TEXT NOT NULL, fingerprint TEXT, status TEXT, "
                   "output_hash TEXT, attempts INTEGER DEFAULT 0, error TEXT, started TEXT, finished TEXT, "
                   "PRIMARY KEY (step, unit))")
        db.execute("INSERT INTO ledger (step, unit, status) VALUES ('code.parse', 'a.adb', 'ok')")
    ledger = Ledger(path)
    ledger.mark("code.parse", "b.adb", "ok", seconds=1.5)
    assert ledger.get("code.parse", "a.adb")["seconds"] is None and ledger.get("code.parse", "b.adb")["seconds"] == 1.5
    assert (tmp_path / "atlas.db.bak-0").exists()


def test_a_workspace_of_a_newer_atlas_is_refused(tmp_path, toml, monkeypatch):
    from atlas.cli import main
    path = tmp_path / "ws" / "atlas.db"
    Store(path.parent)
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(ConfigError, match="this workspace is from a newer atlas: update atlas or use another"):
        Ledger(path)
    with pytest.raises(SystemExit, match="configuration: this workspace is from a newer atlas"):
        main(["status", str(toml())])
    assert version(path) == SCHEMA_VERSION + 1 and list(path.parent.iterdir()) == [path]


def test_a_migration_that_fails_leaves_the_database_as_it_was(tmp_path, monkeypatch):
    path = tmp_path / "atlas.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")

    def broken(db):
        db.execute("ALTER TABLE runs ADD COLUMN half REAL")
        raise RuntimeError("half way")
    monkeypatch.setitem(MIGRATIONS, SCHEMA_VERSION, broken)
    with pytest.raises(RuntimeError, match="half way"):
        connect(path)
    with sqlite3.connect(path) as db:
        assert version(path) == 0 and [r[1] for r in db.execute("PRAGMA table_info(runs)")] == ["id"]
    assert (tmp_path / "atlas.db.bak-0").exists()
    monkeypatch.undo()
    assert schema_version(connect(path)) == SCHEMA_VERSION                 # the next time it does migrate


def test_an_fts_table_is_recreated_with_its_content(tmp_path):
    from atlas.index.store import IndexStore, recreate_fts
    store = IndexStore(tmp_path / "atlas.db")
    if not store.fts:
        pytest.skip("this SQLite has no FTS5")
    records = [{"kind": "requirement", "text": t, "embed_text": t, "ref": ref}
               for ref, t in [("SRS-0001", "The system shall delete a track"), ("SRS-0002", "The display refreshes")]]
    store.replace_document({"doc": "d", "name": "d", "title": "D", "embed_model": "m"}, records,
                           np.zeros((2, 4), dtype=np.float32))
    before = store.lexical("delete track", 5, None)
    with store.db:
        recreate_fts(store.db)
    assert before and store.lexical("delete track", 5, None) == before
    assert store.lexical("SRS-0002", 5, None) == [2]
