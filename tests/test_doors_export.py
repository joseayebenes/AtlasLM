"""tools/doors_export without DOORS: a fake DOORS answers the generated DXL at COM level,
with the length-prefixed protocol, so everything but the DXL itself runs for real."""
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "doors_export"))

import doors_download as dd  # noqa: E402
import publish  # noqa: E402
from raw_store import DoorsObject, Link, RawStore  # noqa: E402

from atlas.doors import ExchangeAdapter  # noqa: E402

DEFS = [("Object Heading", "String", True), ("Object Text", "Text", True), ("Status", "Enum", False),
        ("Created By", "String", True)]


def obj(module, n, ident, text="", heading="", parent=None, level=2, status="Approved", links=()):
    return DoorsObject(module_path=module, absolute_number=n, identifier=ident, outline_number=f"1.{n}",
                       parent_absolute_number=parent, level=level,
                       attributes={"Object Heading": heading, "Object Text": text, "Status": status},
                       links=tuple(links))


def project():
    sat = "/P/Links/Satisfies"
    srs = [obj("/P/SRS", 1, "SRS-1", heading="Track management", level=1),
           obj("/P/SRS", 2, "SRS-2", "The TMF shall delete tracks.", parent=1, links=[Link("/P/SSS", 5, sat)]),
           obj("/P/SRS", 3, "SRS-3", "The TMF shall log.", parent=1,
               links=[Link("/P/SSS", 5, sat), Link("/P/Other", 9, "/P/Links/Refines")]),
           obj("/P/SRS", 4, "SRS-4", "Logs are kept 30 days.", parent=3, level=3)]
    sss = [obj("/P/SSS", 5, "SSS-5", "The system shall manage tracks.", level=1)]
    return {"/P/SRS": srs, "/P/SSS": sss}


class FakeDoors:
    """Answers the DXL of doors_download like DOORS, from objects in memory. The link
    modules of the project are LINK_MODULES; a script with module(l) does not compile, as in
    a real DOORS (the previous result stays)."""
    LINK_MODULES = ["/P/Links/Satisfies", "/P/Links/Refines"]

    def __init__(self, modules):
        self.modules, self.result, self.fail_on_page = modules, "", None

    def runStr(self, script):
        if "module(l)" in script:
            return
        token = re.search(r'ns\("([0-9a-f]{16})"\)', script).group(1)
        fields = ["DKB1", token]
        walk = re.search(r'\(t == "(\w+)"\)', script)
        if walk:                                              # the modules of a kind under a folder
            root = re.search(r'Folder root = folder\("(.*?)"\)', script).group(1)
            found = [m for m in (self.LINK_MODULES if walk.group(1) == "Link" else self.modules)
                     if m.startswith(root + "/")]
            self.result = "".join(f"{len(f)}:{f}" for f in fields + ["MODULES", str(len(found))] + found)
            return
        path = re.search(r'read\("(.*?)", false\)', script).group(1)
        if path not in self.modules:
            fields += ["ERROR", "NO_MODULE"]
        elif '"ATTRS"' in script:
            fields += ["ATTRS", str(len(DEFS))] + [x for n, t, s in DEFS for x in (n, t, "1" if s else "0")]
        elif '"CHECK"' in script:                              # a probe: the first link of its loop, if any
            name = re.search(r'for l in o -> "(.*?)" do', script)
            links = [l for o in self.modules[path] for l in o.links
                     if name and (name.group(1) == "*" or l.link_module == name.group(1))]
            fields += ["CHECK", links[0].target_module if links else "ok"]
        else:
            cursor = re.search(r"object\((\d+), m\)", script)
            size, names = int(re.search(r"emitted >= (\d+)", script).group(1)), re.findall(r'o\."(.+?)" ""', script)
            objs = self.modules[path]
            start = 0 if not cursor else [o.absolute_number for o in objs].index(int(cursor.group(1))) + 1
            if self.fail_on_page is not None and start >= self.fail_on_page:
                self.result = "-R-E- DXL: <Line:12> incorrect arguments for function (parent)"
                return
            page, more = objs[start:start + size], start + size < len(objs)
            fields += ["PAGE", str(page[-1].absolute_number) if more else "", str(len(page))]
            for o in page:
                fields += [str(o.absolute_number), o.identifier, o.outline_number,
                           "" if o.parent_absolute_number is None else str(o.parent_absolute_number), str(o.level)]
                fields += [o.attributes.get(n, "") for n in names]
                found = [(l, name) for name in re.findall(r'for l in o -> "(.*?)" do', script) for l in o.links
                         if name == "*" or l.link_module == name]
                if found or "for l in o" in script:          # each loop: "*" with no link module, the others with theirs
                    fields += [str(len(found))] + [x for l, name in found for x in (
                        l.target_module, str(l.target_absolute_number), "" if name == "*" else name)]
        self.result = "".join(f"{len(f)}:{f}" for f in fields)


def session(modules):
    s = dd.DoorsSession(dxl_timeout=10)
    s._doors = FakeDoors(modules)
    return s


def test_dxl_of_the_page_reads_the_hierarchy_and_the_links():
    script = dd.script_page("/P/SRS", ["Object Text"], "abc123", cursor=17, page_size=5,
                            link_modules=["/P/Links/Satisfies"])
    for piece in ("Object up = parent(o)", "int lv = level(o)", 'for l in o -> "*" do', "targetAbsNo(l)",
                  'for l in o -> "/P/Links/Satisfies" do', 'linkData += ns("/P/Links/Satisfies")',
                  "ModName_ targetMod1 = target(l)", "object(17, m)", "emitted >= 5", 'o."Object Text" ""'):
        assert piece in script, piece
    # module(l) did not compile against a real DOORS ("incorrect arguments for (=)", 2026-10-15)
    assert "module(l)" not in script and not any("module(l)" in body for _, body, _ in dd.CHECKS)
    assert script.count("for l in o") == 2 and dd.script_page("/P/SRS", [], "abc123").count("for l in o") == 1
    assert "for l in o" not in dd.script_page("/P/SRS", [], "abc123", links=False)
    assert dd.escape('/P/"x"\n') == '/P/\\"x\\"\\n'
    assert '(t == "Link")' in dd.script_modules("/P", "abc123", kind="Link")
    assert dd.project_root("/P/Req/SRS") == "/P"
    # no cast in the style of C: "incorrect concatenated token" against a real DOORS (2026-10-15)
    every = [script, dd.script_page("/P/SRS", ["Object Text"], "abc123"), dd.script_modules("/P", "abc123"),
             dd.script_attributes("/P/SRS", "abc123")] + [dd.script_check("/P/SRS", b, "abc123") for _, b, _ in dd.CHECKS]
    assert not any("(int)" in s for s in every)
    assert 'int oAbs = o."Absolute Number"' in script and 'int seenAbs = o."Absolute Number"' in script


def test_download_hierarchy_links_and_incremental(tmp_path):
    db, modules = str(tmp_path / "raw.sqlite"), project()
    s = session(modules)
    first = dd.download(["/P/SRS", "/P/SSS"], db, s, dd.Options(page_size=2))
    assert [(r.seen, r.inserted, r.links, r.pages, r.completed_module) for r in first] == \
        [(4, 4, 3, 2, True), (1, 1, 0, 1, True)]
    with RawStore(db) as store:
        srs = {o.absolute_number: o for o in store.objects("/P/SRS")}
        assert srs[4].parent_absolute_number == 3 and srs[4].level == 3
        assert srs[3].links == (Link("/P/Other", 9, "/P/Links/Refines"), Link("/P/SSS", 5, "/P/Links/Satisfies"))
        assert "Created By" not in srs[2].attributes                           # system attributes left out
        assert store.attribute_names("/P/SRS") == {n for n, _, _ in DEFS}

    # nothing changed; then a new link counts as a change, and a deleted object goes with its links
    assert dd.download(["/P/SRS"], db, s, dd.Options())[0].unchanged == 4
    modules["/P/SRS"][1] = obj("/P/SRS", 2, "SRS-2", "The TMF shall delete tracks.", parent=1,
                               links=[Link("/P/SSS", 5, "/P/Links/Satisfies"), Link("/P/SSS", 6, "/P/Links/Satisfies")])
    del modules["/P/SRS"][2]
    again = dd.download(["/P/SRS"], db, s, dd.Options())[0]
    assert (again.updated, again.unchanged, again.deleted) == (1, 2, 1)
    with RawStore(db) as store:
        assert [o.absolute_number for o in store.objects("/P/SRS")] == [1, 2, 4]
        assert store.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 2


def test_the_links_get_their_link_module_from_the_link_modules_of_the_project(tmp_path):
    """The type of a link comes from its link module: read through each link module of the
    project, and through "*" the others (another project: no link module, the default type)."""
    db, modules = str(tmp_path / "raw.sqlite"), project()
    modules["/P/SRS"][0] = obj("/P/SRS", 1, "SRS-1", heading="Track management", level=1,
                               links=[Link("/Q/SYS", 7, "/Q/Links/Traces")])
    s = session(modules)
    assert s.modules_of("/P", "Link") == FakeDoors.LINK_MODULES
    dd.download(["/P/SRS"], db, s, dd.Options())
    with RawStore(db) as store:
        srs = {o.absolute_number: o for o in store.objects("/P/SRS")}
        assert srs[1].links == (Link("/Q/SYS", 7, ""),)                          # of another project
        assert srs[3].links == (Link("/P/Other", 9, "/P/Links/Refines"), Link("/P/SSS", 5, "/P/Links/Satisfies"))
    untyped = str(tmp_path / "untyped.sqlite")                                   # --no-link-module
    dd.download(["/P/SRS"], untyped, s, dd.Options(link_module=False))
    with RawStore(untyped) as store:
        assert {l.link_module for o in store.objects("/P/SRS") for l in o.links} == {""}


def test_check_probes_the_link_modules_instead_of_module_l():
    results = session(project()).check("/P/SRS")
    assert [(name, ok) for name, ok, _, _ in results] == [
        ("parent(o)", True), ("level(o)", True), ("ModName_ target(l) and fullName", True), ("targetAbsNo(l)", True),
        ("link modules of /P", True), ('o -> "/P/Links/Satisfies"', True), ("a page of the download", True),
        ("a page from a cursor", True)]
    assert results[4][2] == "2: /P/Links/Satisfies, /P/Links/Refines" and results[5][2] == "/P/SSS"
    assert results[6][2] == "1 object, 3 attributes" and results[7][2] == "after 1"


def test_an_interrupted_download_deletes_nothing(tmp_path):
    db, modules = str(tmp_path / "raw.sqlite"), project()
    s = session(modules)
    dd.download(["/P/SRS"], db, s, dd.Options(page_size=2))
    s._doors.fail_on_page = 2                                    # the second page fails
    result = dd.download(["/P/SRS", "/P/Missing"], db, s, dd.Options(page_size=2))
    assert "incorrect arguments" in result[0].error and "could not open the module" in result[1].error
    with RawStore(db) as store:
        assert len(store.objects("/P/SRS")) == 4
        assert store.conn.execute("SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "failed"


def test_a_doors_kb_database_is_upgraded_and_large_modules_work(tmp_path):
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as c:              # the schema of scripts/modelo_sqlite.py (version 1)
        c.executescript("""
            CREATE TABLE modules (module_path TEXT PRIMARY KEY, last_sync_at TEXT, last_full_sync_at TEXT);
            CREATE TABLE requirements (id INTEGER PRIMARY KEY, module_path TEXT NOT NULL, absolute_number INTEGER NOT NULL,
                identifier TEXT NOT NULL DEFAULT '', outline_number TEXT NOT NULL DEFAULT '', heading TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT,
                UNIQUE (module_path, absolute_number));
            CREATE TABLE requirement_attributes (requirement_id INTEGER NOT NULL, name TEXT NOT NULL,
                value_text TEXT NOT NULL DEFAULT '', PRIMARY KEY (requirement_id, name)) WITHOUT ROWID;
            INSERT INTO modules VALUES ('/P/SRS', 'x', 'x');
            INSERT INTO requirements VALUES (1, '/P/SRS', 7, 'SRS-7', '1', '', 'Old text.', 'h', 0, 'x', 'x', 'x', NULL);
            INSERT INTO requirement_attributes VALUES (1, 'Object Text', 'Old text.');
        """)
    with RawStore(str(db)) as store:
        assert store.objects("/P/SRS")[0].attributes == {"Object Text": "Old text."}
        assert store.attribute_names("/P/SRS") == {"Object Text"}            # from the objects: no definitions
        store.register_module("/P/BIG")
        with store.transaction():
            for n in range(40_000):
                store.save(DoorsObject("/P/BIG", n, f"B-{n}"), position=n)
            assert store.mark_missing("/P/BIG", range(1, 40_000)) == 1     # no SQLite variable limit
    assert sqlite3.connect(db).execute("SELECT value FROM meta").fetchone()[0] == "2"


MAPPING = """
[fields]
id = "identifier"
[attributes]
status = "Status"
[modules]
SRS_TMS = "/P/SRS"
[link_types]
satisfies = "Satisfies"
"""


def _published(tmp_path, mapping=MAPPING, **kw):
    raw = str(tmp_path / "raw.sqlite")
    if not Path(raw).exists():
        dd.download(["/P/SRS", "/P/SSS"], raw, session(project()), dd.Options())
    (tmp_path / "mapping.toml").write_text(mapping)
    out = tmp_path / "requirements.sqlite"
    return publish.publish(raw, publish.Mapping.load(tmp_path / "mapping.toml"), str(out), **kw), out


def test_publish_applies_the_mapping(tmp_path):
    reports, out = _published(tmp_path)
    assert [(r.module, r.requirements, r.links, r.unresolved_links) for r in reports] == \
        [("SRS_TMS", 4, 2, 1), ("SSS", 1, 0, 0)]
    assert reports[0].not_downloaded == {"/P/Other": 1}
    ad = ExchangeAdapter({"exchange": {"path": str(out)}})
    reqs = {r.id: r for r in ad.requirements("SRS_TMS")}
    assert reqs["SRS-1"].attributes["kind"] == "heading" and reqs["SRS-1"].text == "Track management"
    assert reqs["SRS-4"].parent == "SRS-3" and reqs["SRS-2"].attributes["status"] == "Approved"
    assert {(l.source, l.target, l.type) for l in ad.links("SRS_TMS")} == \
        {("SRS-2", "SSS-5", "satisfies"), ("SRS-3", "SSS-5", "satisfies")}

    # the ID from an attribute: objects without it are left out, and the parent is the
    # nearest ancestor that has one
    with sqlite3.connect(tmp_path / "raw.sqlite") as c:
        c.execute("INSERT INTO requirement_attributes SELECT id, 'Req ID', 'R-' || absolute_number FROM requirements "
                  "WHERE absolute_number IN (1, 2, 4, 5)")
        c.execute("INSERT INTO attribute_definitions VALUES ('/P/SRS', 'Req ID', 'String', 0)")
    reports, out = _published(tmp_path, MAPPING.replace('id = "identifier"', 'id = "Req ID"'))
    assert (reports[0].requirements, reports[0].without_id) == (3, 1)
    reqs = {r.id: r for r in ExchangeAdapter({"exchange": {"path": str(out)}}).requirements("SRS_TMS")}
    assert reqs["R-4"].parent == "R-1"                                   # SRS-3 has no Req ID

    with sqlite3.connect(tmp_path / "raw.sqlite") as c:                  # two objects with R-2; SSS-5 without ID
        c.execute("UPDATE requirement_attributes SET value_text = 'R-2' WHERE name = 'Req ID' AND value_text = 'R-4'")
        c.execute("DELETE FROM requirement_attributes WHERE name = 'Req ID' AND value_text = 'R-5'")
    reports, out = _published(tmp_path, MAPPING.replace('id = "identifier"', 'id = "Req ID"'))
    assert reports[0].repeated_ids == ["R-2 (2, 4)"]                     # by Absolute Number, to find them
    assert reports[0].summary() == ("SRS_TMS (/P/SRS): 2 objects (requirement 1, heading 1), 0 links; "
                                    "1 objects without an ID left out, 1 repeated IDs left out, "
                                    "1 links to objects without an ID or deleted")


def test_mapping_errors_and_module_selection(tmp_path):
    with pytest.raises(publish.MappingError, match="'Stattus' \\(closest: Status\\)"):
        _published(tmp_path, MAPPING.replace('"Status"', '"Stattus"'))
    with pytest.raises(publish.MappingError, match="unknown section"):
        _published(tmp_path, MAPPING + "[columns]\nx = 1\n")
    with pytest.raises(publish.MappingError, match="given to satisfies and to verifies"):
        _published(tmp_path, MAPPING + 'verifies = "Satisfies"\n')
    reports, out = _published(tmp_path)
    reports, out = _published(tmp_path, modules=["SSS"])                 # only one: the other stays
    assert [r.module for r in reports] == ["SSS"]
    assert ExchangeAdapter({"exchange": {"path": str(out)}}).modules() == ["SRS_TMS", "SSS"]
    reports, out = _published(tmp_path, MAPPING.replace("SRS_TMS", "SRS_NEW"))     # renamed: the old one goes
    assert ExchangeAdapter({"exchange": {"path": str(out)}}).modules() == ["SRS_NEW", "SSS"]


IS_REQUISITE = """
[kinds]
attribute = "is_requisite"
others = "heading_or_information"
[kinds.values]
requirement = "Yes"
"""


def _kinds(out) -> dict[str, str]:
    ad = ExchangeAdapter({"exchange": {"path": str(out)}})
    return {r.id: r.attributes["kind"] for m in ad.modules() for r in ad.requirements(m)}


def test_a_yes_no_attribute_tells_the_requirements_and_the_headings_stay(tmp_path, caplog):
    from atlas.index.records import requirement_records
    caplog.set_level(logging.INFO, logger="doors-publish")
    _published(tmp_path)
    with sqlite3.connect(tmp_path / "raw.sqlite") as c:          # only /P/SRS has it; SRS-1 is the chapter
        c.executemany("INSERT INTO requirement_attributes SELECT id, 'is_requisite', ? FROM requirements "
                      "WHERE module_path = '/P/SRS' AND absolute_number = ?",
                      [("No", 1), ("Yes", 2), (" yes ", 3), ("no", 4)])
        c.execute("INSERT INTO attribute_definitions VALUES ('/P/SRS', 'is_requisite', 'Enum', 0)")
    reports, out = _published(tmp_path, MAPPING + IS_REQUISITE)
    assert _kinds(out) == {"SRS-1": "heading", "SRS-2": "requirement", "SRS-3": "requirement",
                           "SRS-4": "information", "SSS-5": "requirement"}       # SSS, by its content
    assert reports[0].summary() == ("SRS_TMS (/P/SRS): 4 objects (requirement 2, heading 1, information 1), "
                                    "2 links; 1 links to modules not downloaded")
    assert "values of is_requisite not in [kinds.values], taken as heading_or_information: 'No' (1), 'no' (1)" \
        in caplog.text and "/P/SSS: has no is_requisite; its kinds come from the content" in caplog.text
    ad = ExchangeAdapter({"exchange": {"path": str(out)}})               # in atlas, the chapter of each one
    rows = [{"id": r.id, "text": r.text, "parent": r.parent, "attributes": r.attributes}
            for r in ad.requirements("SRS_TMS")]
    assert [(r["kind"], r.get("ref", ""), r["path"]) for r in requirement_records("SRS_TMS", rows)] == [
        ("requirement", "SRS-2", "1.1 Track management"), ("requirement", "SRS-3", "1.1 Track management"),
        ("chunk", "", "1.1 Track management")]

    # without others, what is not "Yes" is a requirement, the chapter too, and publish.py warns
    caplog.clear()
    _, out = _published(tmp_path, MAPPING + IS_REQUISITE.replace('others = "heading_or_information"\n', ""))
    assert _kinds(out)["SRS-1"] == "requirement"
    assert [r.levelname for r in caplog.records if "not in [kinds.values]" in r.getMessage()] == ["WARNING"]
    # "No" as information: the chapter is information, and its requirements lose it
    _, out = _published(tmp_path, MAPPING + IS_REQUISITE + 'information = "NO"\n')
    assert (_kinds(out)["SRS-1"], _kinds(out)["SRS-4"]) == ("information", "information")


def test_the_requirement_name_as_id_and_another_one_for_the_objects_without_it(tmp_path, caplog):
    from atlas.index.records import requirement_records
    _published(tmp_path)
    with sqlite3.connect(tmp_path / "raw.sqlite") as c:          # only requirements have a name, SRS-3 not yet
        c.executemany("INSERT INTO requirement_attributes SELECT id, ?, ? FROM requirements "
                      "WHERE module_path = '/P/SRS' AND absolute_number = ?",
                      [("is_requisite", "No", 1), ("is_requisite", "Yes", 2), ("is_requisite", "Yes", 3),
                       ("is_requisite", "No", 4), ("unique-identifier", "TMS-REQ-002", 2)])
        c.executemany("INSERT INTO attribute_definitions VALUES ('/P/SRS', ?, 'String', 0)",
                      [("is_requisite",), ("unique-identifier",)])
    mapping = MAPPING.replace('id = "identifier"', 'id = ["unique-identifier", "module_number"]') + IS_REQUISITE
    reports, out = _published(tmp_path, mapping)
    assert _kinds(out) == {"SRS_TMS-1": "heading", "TMS-REQ-002": "requirement", "SRS_TMS-3": "requirement",
                           "SRS_TMS-4": "information", "SSS-5": "requirement"}
    assert "SRS_TMS: 1 requirements without unique-identifier, published with another ID: SRS_TMS-3" in caplog.text
    caplog.clear()
    ad = ExchangeAdapter({"exchange": {"path": str(out)}, "convention": {"id_pattern": r"^TMS-REQ-\d{3}$"}})
    assert {(l.source, l.target, l.type) for l in ad.links("SRS_TMS")} == \
        {("TMS-REQ-002", "SSS-5", "satisfies"), ("SRS_TMS-3", "SSS-5", "satisfies")}
    rows = [{"id": r.id, "text": r.text, "parent": r.parent, "attributes": r.attributes}
            for r in ad.requirements("SRS_TMS")]
    assert [m for m in caplog.messages if "outside the convention" in m] == \
        [r"ID outside the convention (^TMS-REQ-\d{3}$): 'SRS_TMS-3' in SRS_TMS"]     # not the chapter
    assert [(r["kind"], r.get("ref", ""), r["path"]) for r in requirement_records("SRS_TMS", rows)] == [
        ("requirement", "TMS-REQ-002", "1.1 Track management"), ("requirement", "SRS_TMS-3", "1.1 Track management"),
        ("chunk", "", "1.1 Track management")]
    for bad in ("[]", '["unique-identifier", 3]'):
        (tmp_path / "bad.toml").write_text(MAPPING.replace('id = "identifier"', f"id = {bad}"))
        with pytest.raises(publish.MappingError, match="id must be a text or a list of texts"):
            publish.Mapping.load(tmp_path / "bad.toml")


@pytest.mark.parametrize("kinds, error", [
    ('attribute = "is_requisite"\nothers = "maybe"\n', "others is 'maybe'"),
    ('attribute = "is_requisite"\n[kinds.values]\nrequirement = "Yes"\ninformation = "yes"\n',
     "'yes' is given to requirement and to information"),
    ('others = "information"\n', "need attribute"),
    ('attribute = "is_requisite"\n[kinds.values]\nrequirement = true\n', "must be a text or a list of texts")])
def test_kinds_mapping_errors(tmp_path, kinds, error):
    (tmp_path / "mapping.toml").write_text(MAPPING + "[kinds]\n" + kinds)
    with pytest.raises(publish.MappingError, match=error):
        publish.Mapping.load(tmp_path / "mapping.toml")


def test_from_doors_to_atlas_run(tmp_path, toml, capsys):
    from atlas.cli import run
    from atlas.config import load_config
    from atlas.store import Store
    _, out = _published(tmp_path)
    cfg_path = tmp_path / "exchange.toml"
    cfg_path.write_text(toml().read_text().split("[sources.doors]")[0] + f'''
[pipeline]
steps = ["doors"]
[sources.doors]
adapter = "exchange"
[sources.doors.exchange]
path = "{out.as_posix()}"
''')
    cfg = load_config(cfg_path)
    assert run(cfg) == 0 and json.loads(capsys.readouterr().out)["processed"] == {"doors.requirements": 2}
    req = Store(cfg.workspace).requirement("SRS-3")
    assert req["parent"] == "SRS-1" and req["links_out"] == [{"target": "SSS-5", "type": "satisfies"}]
