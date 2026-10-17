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


# --- tools/reqif_import ----------------------------------------------------------------------

def _reqif_tool():
    import sys
    from pathlib import Path
    tools = Path(__file__).resolve().parents[1] / "tools"
    sys.path.insert(0, str(tools / "reqif_import"))
    import reqif_to_exchange
    return reqif_to_exchange, tools / "reqif_import" / "mapping.example.toml"


def _rows(db, columns="module, id, text, heading, kind, parent, position, level, attributes"):
    with sqlite3.connect(db) as c:
        return (c.execute(f"SELECT {columns} FROM exchange_requirements ORDER BY id").fetchall(),
                c.execute("SELECT * FROM exchange_links ORDER BY source, target, type").fetchall())


def test_the_reqif_of_the_samples_gives_the_rows_of_make_samples(samples, tmp_path):
    from examples import make_samples
    tool, example = _reqif_tool()
    reqif = make_samples.reqif(tmp_path / "samples.reqif")
    db = tmp_path / "requirements.sqlite"
    reports = tool.import_reqif([reqif], tool.Mapping.load(example), db, id_pattern=r"^(?:SRS|SSS)-\d{4}$")
    assert [(r.module, r.requirements, r.links, r.error) for r in reports] == \
        [("SRS_TMS", 41, 40, ""), ("SRS_TMS_OLD", 3, 0, ""), ("SSS_MISSION", 13, 0, "")]
    assert _rows(db) == _rows(samples / "requirements.sqlite")     # all but the outline and source_key
    outline = dict(_rows(db, "id, outline")[0])
    assert (outline["SRS-0900"], outline["SRS-0006"], outline["SRS-0007"]) == ("1", "1.6", "1.6.1")   # from the hierarchy
    meta = dict(sqlite3.connect(db).execute("SELECT key, value FROM exchange_meta").fetchall())
    assert meta["filler"] == "tools/reqif_import"


NS = 'xmlns="http://www.omg.org/spec/ReqIF/20110401/reqif.xsd" xmlns:xhtml="http://www.w3.org/1999/xhtml"'


def _value(kind, definition, value):
    if kind == "XHTML":
        return (f'<ATTRIBUTE-VALUE-XHTML><DEFINITION><ATTRIBUTE-DEFINITION-XHTML-REF>{definition}'
                f'</ATTRIBUTE-DEFINITION-XHTML-REF></DEFINITION><THE-VALUE>{value}</THE-VALUE></ATTRIBUTE-VALUE-XHTML>')
    if kind == "ENUMERATION":
        refs = "".join(f"<ENUM-VALUE-REF>{v}</ENUM-VALUE-REF>" for v in value)
        return (f'<ATTRIBUTE-VALUE-ENUMERATION><DEFINITION><ATTRIBUTE-DEFINITION-ENUMERATION-REF>{definition}'
                f'</ATTRIBUTE-DEFINITION-ENUMERATION-REF></DEFINITION><VALUES>{refs}</VALUES></ATTRIBUTE-VALUE-ENUMERATION>')
    return (f'<ATTRIBUTE-VALUE-STRING THE-VALUE="{value}"><DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>{definition}'
            f'</ATTRIBUTE-DEFINITION-STRING-REF></DEFINITION></ATTRIBUTE-VALUE-STRING>')


def _object(ident, type_, *values):
    return (f'<SPEC-OBJECT IDENTIFIER="{ident}"><TYPE><SPEC-OBJECT-TYPE-REF>{type_}</SPEC-OBJECT-TYPE-REF></TYPE>'
            f'<VALUES>{"".join(_value(*v) for v in values)}</VALUES></SPEC-OBJECT>')


def _hierarchy(ident, *children):
    inner = f'<CHILDREN>{"".join(children)}</CHILDREN>' if children else ""
    return (f'<SPEC-HIERARCHY IDENTIFIER="h-{ident}"><OBJECT><SPEC-OBJECT-REF>{ident}</SPEC-OBJECT-REF></OBJECT>'
            f'{inner}</SPEC-HIERARCHY>')


def _relation(n, kind, source, target):
    return (f'<SPEC-RELATION IDENTIFIER="r{n}"><TYPE><SPEC-RELATION-TYPE-REF>{kind}</SPEC-RELATION-TYPE-REF></TYPE>'
            f'<SOURCE><SPEC-OBJECT-REF>{source}</SPEC-OBJECT-REF></SOURCE>'
            f'<TARGET><SPEC-OBJECT-REF>{target}</SPEC-OBJECT-REF></TARGET></SPEC-RELATION>')


def _reqif(datatypes, spec_types, objects, relations, specifications):
    return (f'<?xml version="1.0" encoding="UTF-8"?><REQ-IF {NS}><THE-HEADER><REQ-IF-HEADER IDENTIFIER="h"/>'
            f'</THE-HEADER><CORE-CONTENT><REQ-IF-CONTENT><DATATYPES>{datatypes}</DATATYPES>'
            f'<SPEC-TYPES>{spec_types}</SPEC-TYPES><SPEC-OBJECTS>{objects}</SPEC-OBJECTS>'
            f'<SPEC-RELATIONS>{relations}</SPEC-RELATIONS><SPECIFICATIONS>{specifications}</SPECIFICATIONS>'
            f'</REQ-IF-CONTENT></CORE-CONTENT></REQ-IF>')


def _definition(kind, ident, name, extra=""):
    return f'<ATTRIBUTE-DEFINITION-{kind} IDENTIFIER="{ident}" LONG-NAME="{name}">{extra}</ATTRIBUTE-DEFINITION-{kind}>'


ENUMS = ('<DATATYPE-DEFINITION-ENUMERATION IDENTIFIER="dt-kind"><SPECIFIED-VALUES>'
         + "".join(f'<ENUM-VALUE IDENTIFIER="k-{v}" LONG-NAME="{v}"/>' for v in ("Requirement", "Heading", "Note", "Funny"))
         + '</SPECIFIED-VALUES></DATATYPE-DEFINITION-ENUMERATION><DATATYPE-DEFINITION-ENUMERATION IDENTIFIER="dt-tags">'
         '<SPECIFIED-VALUES><ENUM-VALUE IDENTIFIER="t-a" LONG-NAME="Timing"/><ENUM-VALUE IDENTIFIER="t-b">'
         '<PROPERTIES><EMBEDDED-VALUE KEY="1" OTHER-CONTENT="Safety"/></PROPERTIES></ENUM-VALUE></SPECIFIED-VALUES>'
         '</DATATYPE-DEFINITION-ENUMERATION>')
SRS_TYPE = ('<SPEC-OBJECT-TYPE IDENTIFIER="t-srs" LONG-NAME="SRS object"><SPEC-ATTRIBUTES>'
            + _definition("STRING", "d-id", "ID") + _definition("XHTML", "d-text", "ReqIF.Text")
            + _definition("STRING", "d-heading", "ReqIF.ChapterName")
            + _definition("ENUMERATION", "d-kind", "Object Type") + _definition("ENUMERATION", "d-tags", "Tags")
            + _definition("STRING", "d-safety", "Safety", '<DEFAULT-VALUE><ATTRIBUTE-VALUE-STRING THE-VALUE="none">'
                          '<DEFINITION><ATTRIBUTE-DEFINITION-STRING-REF>d-safety</ATTRIBUTE-DEFINITION-STRING-REF>'
                          '</DEFINITION></ATTRIBUTE-VALUE-STRING></DEFAULT-VALUE>')
            + '</SPEC-ATTRIBUTES></SPEC-OBJECT-TYPE><SPEC-RELATION-TYPE IDENTIFIER="rt-sat" LONG-NAME="Satisfaction"/>'
            '<SPEC-RELATION-TYPE IDENTIFIER="rt-ref" LONG-NAME="Refined By"/>')
TEXT = ('<xhtml:div><xhtml:p>The TMF shall <xhtml:b>delete</xhtml:b>\n   a track:</xhtml:p>'
        '<xhtml:ul><xhtml:li>on request,</xhtml:li><xhtml:li>when lost.</xhtml:li></xhtml:ul></xhtml:div>')
SRS = _reqif(
    ENUMS, SRS_TYPE,
    _object("h1", "t-srs", ("STRING", "d-id", "SRS-0100"), ("STRING", "d-heading", "Deletion"),
            ("ENUMERATION", "d-kind", ["k-Heading"]))
    + _object("o1", "t-srs", ("STRING", "d-id", "SRS-0001"), ("XHTML", "d-text", TEXT),
              ("ENUMERATION", "d-kind", ["k-Requirement"]), ("ENUMERATION", "d-tags", ["t-a", "t-b"]))
    + _object("o2", "t-srs", ("XHTML", "d-text", "<xhtml:p>A note without an ID.</xhtml:p>"),
              ("ENUMERATION", "d-kind", ["k-Note"]))
    + _object("o3", "t-srs", ("STRING", "d-id", "SRS-0002"), ("XHTML", "d-text", "<xhtml:p>Logs.</xhtml:p>"),
              ("ENUMERATION", "d-kind", ["k-Requirement"]), ("STRING", "d-safety", "SIL2"))
    + _object("o4", "t-srs", ("STRING", "d-id", "SRS-0001"), ("XHTML", "d-text", "<xhtml:p>Again.</xhtml:p>"))
    + _object("o5", "t-srs", ("STRING", "d-id", "SRS-12"), ("XHTML", "d-text", "<xhtml:p>Odd.</xhtml:p>"),
              ("ENUMERATION", "d-kind", ["k-Funny"])),
    _relation(1, "rt-sat", "o1", "s1") + _relation(2, "rt-ref", "o3", "o1") + _relation(3, "rt-ref", "o1", "gone"),
    '<SPECIFICATION IDENTIFIER="spec-srs" LONG-NAME="Software Requirements"><CHILDREN>'
    + _hierarchy("h1", _hierarchy("o1"), _hierarchy("o2", _hierarchy("o3")), _hierarchy("o4")) + _hierarchy("o5")
    + '</CHILDREN></SPECIFICATION>')
SSS = _reqif(
    "", '<SPEC-OBJECT-TYPE IDENTIFIER="t-sss"><SPEC-ATTRIBUTES>' + _definition("STRING", "e-id", "ID")
    + _definition("XHTML", "e-text", "ReqIF.Text") + '</SPEC-ATTRIBUTES></SPEC-OBJECT-TYPE>',
    _object("s1", "t-sss", ("STRING", "e-id", "SSS-0001"), ("XHTML", "e-text", "The system shall track.")), "",
    '<SPECIFICATION IDENTIFIER="spec-sss" LONG-NAME="SSS"><CHILDREN>' + _hierarchy("s1") + '</CHILDREN></SPECIFICATION>')
MAPPING = '''
[fields]
id = "ID"                    # text and heading: "Object Text" and "Object Heading", the defaults of DOORS
[attributes]
tags = "Tags"
safety = "Safety"
[modules]
SRS_TMS = "Software Requirements"
[link_types]
satisfies = "Satisfaction"
[kinds]
attribute = "Object Type"
[kinds.values]
requirement = "Requirement"
heading = "Heading"
information = "Note"
'''


def test_reqif_values_hierarchy_kinds_links_and_warnings(tmp_path, caplog):
    import logging
    import zipfile
    tool, _ = _reqif_tool()
    (tmp_path / "srs.reqif").write_text(SRS, encoding="utf-8")
    with zipfile.ZipFile(tmp_path / "sss.reqifz", "w") as z:            # a .reqifz: a zip with the .reqif
        z.writestr("sss.reqif", SSS)
    (tmp_path / "mapping.toml").write_text(MAPPING)
    mapping = tool.Mapping.load(tmp_path / "mapping.toml")
    db = tmp_path / "requirements.sqlite"
    caplog.set_level(logging.INFO)
    srs, sss = tool.import_reqif([tmp_path / "srs.reqif", tmp_path / "sss.reqifz"], mapping, db,
                                 id_pattern=r"^(?:SRS|SSS)-\d{4}$")
    assert srs.summary() == ("SRS_TMS (srs.reqif#Software Requirements): 4 objects (requirement 3, heading 1), "
                             "2 links; 1 objects without an ID left out, 1 repeated IDs left out, "
                             "1 links to objects that are in none of the files")
    assert srs.repeated_ids == ["SRS-0001 (o1, o4)"] and srs.out_of_pattern == ["SRS-12"]
    rows = {r[0]: r[1:] for r in _rows(db, "id, text, heading, kind, parent, position, level, outline, attributes")[0]}
    assert rows["SRS-0100"] == ("", "Deletion", "heading", None, 1, 1, "1", '{"safety": "none", "tags": ""}')
    assert rows["SRS-0001"] == ("The TMF shall delete a track:\non request,\nwhen lost.", "", "requirement",
                                "SRS-0100", 2, 2, "1.1", '{"safety": "none", "tags": "Timing, Safety"}')
    assert rows["SRS-0002"][3:7] == ("SRS-0100", 4, 3, "1.2.1")              # its parent without an ID is skipped
    assert rows["SRS-12"][2:4] == ("requirement", None)                       # "Funny": no kind, a requirement
    assert rows["SSS-0001"][:3] == ("The system shall track.", "", "requirement") and rows["SSS-0001"][-1] == "{}"
    assert _rows(db)[1] == [("SRS-0001", "SSS-0001", "satisfies", "SRS_TMS", "SSS"),
                            ("SRS-0002", "SRS-0001", "refined_by", "SRS_TMS", "SRS_TMS")]
    log = caplog.text
    assert "SRS_TMS: 1 repeated IDs left out (IDENTIFIER of the object published and of the one left out)" in log
    assert "SRS_TMS: 1 IDs do not match" in log and "SRS-12" in log
    assert "values of Object Type not in [kinds.values], taken as requirement: 'Funny' (1)" in log
    assert "SRS_TMS: links to objects that are in none of the files: gone (1)" in log
    assert "SSS: has no Object Type, Safety, Tags; its kinds come from the content" in log

    caplog.clear()                                       # the SRS alone: its link to the SSS has no target now
    srs, = tool.import_reqif([tmp_path / "srs.reqif"], mapping, db)
    assert srs.links == 1 and srs.not_downloaded == {"s1": 1, "gone": 1}
    assert "SSS: no longer imported, removed" in caplog.text
    (tmp_path / "bad.toml").write_text('[attributes]\nsafety = "Safty"\n')
    with pytest.raises(tool.MappingError, match="'Safty' \\(closest: Safety\\)"):
        tool.import_reqif([tmp_path / "srs.reqif"], tool.Mapping.load(tmp_path / "bad.toml"), db)
    (tmp_path / "not.reqif").write_text("<html/>")
    assert tool.main([str(tmp_path / "not.reqif"), "--out", str(db)]) == 1
    assert "not a ReqIF file" in caplog.text


def test_atlas_run_over_an_imported_reqif(samples, toml, tmp_path, fake, capsys):
    from examples import make_samples
    from atlas.cli import run, search
    from atlas.config import load_config
    from atlas.store import Store
    tool, example = _reqif_tool()
    db = tmp_path / "requirements.sqlite"
    assert tool.main([str(make_samples.reqif(tmp_path / "samples.reqif")), "--mapping", str(example),
                      "--out", str(db)]) == 0
    capsys.readouterr()
    cfg_path = tmp_path / "reqif.toml"          # the [project] of the test TOML, with the imported database
    cfg_path.write_text(toml().read_text().split("[sources.doors]")[0] + f'''
[llm]
base_url = "{fake[0]}"
model = "fake"
[embeddings]
model = "embeddinggemma"
[network]
proxy = ""
no_proxy = "*"
[pipeline]
steps = ["doors", "graph", "index"]
[sources.doors]
adapter = "exchange"
modules = ["SSS_MISSION", "SRS_TMS"]
[sources.doors.exchange]
path = "{db.as_posix()}"
[sources.doors.convention]
id_pattern = "^(?:SRS|SSS)-\\\\d{{4}}$"
''')
    cfg = load_config(cfg_path)
    assert run(cfg) == 0
    assert json.loads(capsys.readouterr().out)["processed"]["doors.requirements"] == 2
    req = Store(cfg.workspace).requirement("SRS-0007")
    assert req["parent"] == "SRS-0006" and {"target": "SSS-0008", "type": "satisfies"} in req["links_out"]
    assert search(cfg, "SRS-0007") == 0 and "SRS-0007: The TMF shall process track event 7" in capsys.readouterr().out
