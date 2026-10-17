import json
import sqlite3

import pytest

from atlas.doors import ExchangeAdapter
from atlas.doors.exchange import SCHEMA_VERSION, ExchangeError, ExchangeWriter

REQS = [{"id": "SRS-0900", "heading": "Track management", "kind": "heading", "level": 1},
        {"id": "SRS-0001", "text": "The TMF shall delete a track (see SRS-0002).", "parent": "SRS-0900", "level": 2,
         "attributes": {"status": "Approved"}},
        {"id": "srs-0002", "text": "The TMF shall log.", "parent": "SRS-0900", "level": 2}]
LINKS = [{"source": "SRS-0001", "target": "SSS-0003", "type": "satisfies", "target_module": "SSS_MISSION"}]


def _adapter(path, **extra):
    return ExchangeAdapter({"exchange": {"path": str(path), **extra},
                            "convention": {"id_pattern": r"^SRS-\d{4}$", "inline_reference_regex": r"\bSRS-\d{4}\b"}})


def test_written_and_read_back(tmp_path):
    db = tmp_path / "requirements.sqlite"
    with ExchangeWriter(db, filler="test") as w:
        assert w.replace_module("SRS_TMS", REQS, LINKS, source="/TMS/SRS") == []
    ad = _adapter(db)
    assert ad.modules() == ["SRS_TMS"]
    reqs = list(ad.requirements("SRS_TMS"))
    assert [r.id for r in reqs] == ["SRS-0900", "SRS-0001", "SRS-0002"]          # in module order, normalised
    assert reqs[0].text == "Track management" and reqs[0].attributes["kind"] == "heading"
    assert reqs[1].parent == "SRS-0900" and reqs[1].attributes == {"status": "Approved", "kind": "requirement",
                                                                 "heading": "", "outline": "", "level": 2}
    assert {(l.source, l.target, l.type) for l in ad.links("SRS_TMS")} == \
        {("SRS-0001", "SSS-0003", "satisfies"), ("SRS-0001", "SRS-0002", "references")}
    assert [r.id for r in _adapter(db, kinds=["requirement"]).requirements("SRS_TMS")] == ["SRS-0001", "SRS-0002"]
    meta = dict(sqlite3.connect(db).execute("SELECT key, value FROM exchange_meta").fetchall())
    assert meta["schema_version"] == str(SCHEMA_VERSION) and meta["filler"] == "test"
    filled = sqlite3.connect(db).execute("SELECT filled_at FROM exchange_modules").fetchone()[0]
    assert ad.module_info("SRS_TMS") == {"exported_at": filled, "source": "/TMS/SRS"} and ad.exported_at("SRS_TMS") == filled
    assert ad.module_info("NOPE") == {"exported_at": "", "source": ""}


def test_the_snapshot_changes_only_with_the_content(tmp_path):
    db = tmp_path / "requirements.sqlite"
    with ExchangeWriter(db, filler="test") as w:
        w.replace_module("SRS_TMS", REQS, LINKS)
        first = _adapter(db).snapshot("SRS_TMS")
        w.replace_module("SRS_TMS", REQS, LINKS)                        # the same again
        assert _adapter(db).snapshot("SRS_TMS") == first
        w.replace_module("SRS_TMS", REQS, [])                           # a link less
        assert _adapter(db).snapshot("SRS_TMS") != first
        w.remove_module("SRS_TMS")
        assert w.modules() == []


@pytest.mark.parametrize("reqs, links, error", [
    ([{"text": "no id"}], [], "has no id"),
    ([{"id": "A-1"}, {"id": "A-1"}], [], "repeated"),
    ([{"id": "A-1", "kind": "chapter"}], [], "kind"),
    ([{"id": "A-1", "attributes": {"level": "3"}}], [], "reserved"),
    ([{"id": "A-1"}], [{"source": "B-1", "target": "A-1", "type": "satisfies"}], "starts outside"),
    ([{"id": "A-1"}], [{"source": "A-1", "target": "B-1"}], "without source, target or type"),
])
def test_the_contract_is_checked(tmp_path, reqs, links, error):
    with ExchangeWriter(tmp_path / "x.sqlite", filler="test") as w:
        with pytest.raises(ExchangeError, match=error):
            w.replace_module("M", reqs, links)
        assert w.modules() == []                                       # nothing written


def test_ids_are_unique_in_the_database_and_a_failed_write_keeps_the_module(tmp_path):
    db = tmp_path / "x.sqlite"
    with ExchangeWriter(db, filler="test") as w:
        w.replace_module("A", [{"id": "X-1", "text": "first"}])
        w.replace_module("B", [{"id": "Y-1"}])
        with pytest.raises(ExchangeError, match="unique"):
            w.replace_module("B", [{"id": "X-1"}])
        assert [r.id for r in _adapter(db).requirements("B")] == ["Y-1"]     # B as it was
        assert w.replace_module("A", [{"id": "X-1", "parent": "Z-9"}]) == ["A: the parent Z-9 of X-1 is not in the module"]


def test_other_databases_are_refused(tmp_path):
    other = tmp_path / "other.sqlite"
    sqlite3.connect(other).execute("CREATE TABLE t (x)")
    with pytest.raises(ExchangeError, match="not a requirements exchange database"):
        _adapter(other).modules()
    db = tmp_path / "future.sqlite"
    ExchangeWriter(db, filler="test").close()
    with sqlite3.connect(db) as c:
        c.execute("UPDATE exchange_meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(ExchangeError, match="version 99"):
        _adapter(db).modules()
    with pytest.raises(ValueError, match="kinds"):
        _adapter(db, kinds=["chapter"])


def test_atlas_run_reads_the_exchange_database(toml, samples, tmp_path, capsys):
    import shutil
    from atlas.cli import run
    from atlas.config import load_config
    from atlas.store import Store
    db = tmp_path / "requirements.sqlite"
    shutil.copy(samples / "requirements.sqlite", db)
    cfg_path = tmp_path / "exchange.toml"          # the [project] of the test TOML, with an exchange source
    cfg_path.write_text(toml().read_text().split("[sources.doors]")[0] + f'''
[pipeline]
steps = ["doors"]
[sources.doors]
adapter = "exchange"
modules = ["SSS_MISSION", "SRS_TMS"]
[sources.doors.exchange]
path = "{db.as_posix()}"
[sources.doors.convention]
id_pattern = "^(?:SRS|SSS)-\\\\d{{4}}$"
''')
    cfg = load_config(cfg_path)
    assert run(cfg) == 0 and json.loads(capsys.readouterr().out)["processed"] == {"doors.requirements": 2}
    req = Store(cfg.workspace).requirement("SRS-0007")
    assert req["parent"] == "SRS-0006" and req["attributes"]["status"] in ("Approved", "Draft")
    assert {"target": "SSS-0008", "type": "satisfies"} in req["links_out"]
    assert run(cfg) == 0 and json.loads(capsys.readouterr().out)["processed"] == {}
    with ExchangeWriter(db, filler="test") as w:                    # a filler publishes again
        w.replace_module("SRS_TMS", [{"id": "SRS-0001", "text": "Only one now."}])
    assert run(cfg) == 0 and json.loads(capsys.readouterr().out)["processed"] == {"doors.requirements": 1}
    assert Store(cfg.workspace).requirement("SRS-0007") is None


def test_the_date_of_the_export_is_kept_shown_and_warned(toml, samples, tmp_path, capsys, caplog):
    """When and from where each module was exported: in atlas.db after reading it, read again
    when only the date changes (a new export of the same content), and a warning when it is
    older than [sources.doors] max_age_days."""
    import shutil
    from datetime import datetime, timedelta, timezone
    from atlas.cli import run, status
    from atlas.config import ConfigError, load_config
    from atlas.doors import describe_exports, export_age, stale_exports
    from atlas.store import Store
    db = tmp_path / "requirements.sqlite"
    shutil.copy(samples / "requirements.sqlite", db)
    base = toml().read_text().split("[sources.doors]")[0] + f'''
[pipeline]
steps = ["doors"]
[sources.doors]
adapter = "exchange"
modules = ["SSS_MISSION", "SRS_TMS"]
{{extra}}
[sources.doors.exchange]
path = "{db.as_posix()}"
'''
    cfg_path = tmp_path / "exchange.toml"

    def config(extra=""):
        cfg_path.write_text(base.replace("{extra}", extra))
        return load_config(cfg_path)

    cfg = config()
    assert run(cfg) == 0
    capsys.readouterr()
    info = Store(cfg.workspace).modules_info()
    filled = dict(sqlite3.connect(db).execute("SELECT module, filled_at FROM exchange_modules").fetchall())
    assert info["SRS_TMS"]["exported_at"] == filled["SRS_TMS"] and info["SRS_TMS"]["source"] == "/Mission/SRS_TMS"
    assert info["SRS_TMS"]["run_started"] and set(info) == {"SSS_MISSION", "SRS_TMS"}

    # the filler publishes the same content again, 40 days ago: only the date changed
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE exchange_modules SET filled_at = ? WHERE module = 'SRS_TMS'", (old,))
    caplog.clear()
    assert run(config("max_age_days = 30")) == 0
    assert json.loads(capsys.readouterr().out)["processed"] == {"doors.requirements": 1}
    assert Store(cfg.workspace).modules_info()["SRS_TMS"]["exported_at"] == old
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("SRS_TMS was exported" in w and "40 days ago" in w for w in warned)
    assert not any("SSS_MISSION" in w for w in warned)
    assert run(config("max_age_days = 60")) == 0                       # display only: nothing read again
    assert json.loads(capsys.readouterr().out)["processed"] == {}

    assert status(cfg) == 0
    assert f"DOORS: 2 modules, exported {old[:10]} to " in capsys.readouterr().out
    assert export_age(old) == 40 and export_age("") is None and export_age("not a date") is None
    assert describe_exports({"A": {"exported_at": old}}) == f"1 module, exported {old[:10]}, 40 days ago"
    assert describe_exports({"A": {"exported_at": ""}, "B": {}}) == "2 modules, export date unknown"
    assert stale_exports({"A": {"exported_at": old}}, 0) == [] and stale_exports({"A": {"exported_at": old}}, 39)
    with pytest.raises(ConfigError, match="max_age_days"):
        config("max_age_days = -1")
