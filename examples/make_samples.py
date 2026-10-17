#!/usr/bin/env python3
"""Generates sample data in examples/samples/ to develop and test without access to real
documents, code or DOORS:

  samples/docs/spec_with_bookmarks.pdf      specification with a navigable outline (bookmarks)
  samples/docs/spec_without_bookmarks.pdf   the same one, without bookmarks and with split numbers
  samples/docs/hmi_manual.md                a Markdown file with requirements
  samples/ada/src/*.ads, *.adb              small Ada project with Req: comments
  samples/ada/samples.gpr                   its GPR project: a scenario variable, src and tests
  samples/doors.sqlite                      DOORS operational copy: modules, requirements, links
  samples/requirements.sqlite               the same requirements in the exchange database of atlas
                                            (atlas.doors.exchange), with headings and hierarchy
  samples/requirements.reqif                those of requirements.sqlite as a ReqIF file, for
                                            tools/reqif_import (with its mapping.example.toml)

Requires: pip install pymupdf
"""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "samples"


def pdfs() -> None:
    import pymupdf
    random.seed(1)
    (ROOT / "docs").mkdir(parents=True, exist_ok=True)

    def build(name: str, with_bookmarks: bool) -> None:
        doc = pymupdf.open()
        state = {"page": None, "y": 0, "n": 0}
        toc = []

        def new_page():
            state["page"] = doc.new_page(width=595, height=842)
            state["n"] += 1
            state["page"].insert_text((72, 40), "COMPANY - Company Confidential", fontsize=8)
            state["page"].insert_text((72, 52), "Doc. TMS-SRS-001  Issue 2", fontsize=8)
            state["page"].insert_text((280, 815), f"Page {state['n']} of 99", fontsize=8)
            state["y"] = 90

        def line(text, x=72, size=10, gap=14):
            if state["y"] > 780:
                new_page()
            state["page"].insert_text((x, state["y"]), text, fontsize=size)
            state["y"] += gap

        new_page()
        line("Track Management Software Requirements Specification", size=16, gap=30)
        line("Table of Contents", size=12, gap=20)
        for c in range(1, 4):
            line(f"{c} Chapter {c} title ........................ {c * 3}")
        new_page()
        n = 0
        for c in range(1, 4):
            line(f"{c}", size=14, gap=0); line(f"Chapter {c} Functions", x=110, size=14, gap=24)
            toc.append([1, f"{c} Chapter {c} Functions", state["n"]])
            for s1 in range(1, 4):
                line(f"{c}.{s1}", size=12, gap=0); line(f"Function group {c}.{s1}", x=130, size=12, gap=20)
                toc.append([2, f"{c}.{s1} Function group {c}.{s1}", state["n"]])
                for s2 in range(1, 4):
                    num = f"{c}.{s1}.{s2}"
                    line(num, size=11, gap=0); line(f"Track handling {num}", x=150, size=11, gap=18)
                    for r in range(random.randint(4, 8)):
                        n += 1
                        line(f"Req: SRS-{n:04d} The TMF shall process track event {r} of")
                        line(f"group {num} within {random.randint(10, 500)} ms after reception (see SSS-{(n % 12) + 1:04d}).", gap=16)
        if with_bookmarks:
            doc.set_toc(toc)
        doc.save(ROOT / "docs" / name)
        print("created", ROOT / "docs" / name, state["n"], "pages")

    build("spec_with_bookmarks.pdf", True)
    build("spec_without_bookmarks.pdf", False)
    (ROOT / "docs" / "hmi_manual.md").write_text(
        "# HMI Manual\n\n1 General\n\nReq: SRS-0101 The HMI shall display tracks.\n\n"
        "2 Alarms\n\nReq: SRS-0102 The HMI shall raise an alarm when a track is lost. Alarm latency is TBD.\n\n"
        "3 Logging\n\nReq: SRS-0103 The HMI shall keep logs for 30 days.\n", encoding="utf-8")


def ada() -> None:
    src = ROOT / "ada" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "tracks.ads").write_text("""package Tracks is
   -- Record of a radar track.
   -- Req: SRS-0001
   type Track is record
      Id   : Integer;
      Az   : Float;
   end record;
   type Track_Array is array (1 .. 100) of Track;
   subtype Hour_Range is Integer range 0 .. 23;
   Trk_DB : Track_Array;
   Max_Tracks : constant Integer := 100;
   -- Updates track I.
   -- Req: SRS-0002, SRS-0003
   procedure Update (I : Integer);
   -- Removes track I from the database.
   -- Req: SRS-0004
   procedure Delete_Track (I : Integer);
end Tracks;
""", encoding="utf-8")
    (src / "tracks.adb").write_text("""with Logger;
package body Tracks is
   procedure Update (I : Integer) is
      H : Hour_Range;
   begin
      H := Hour_Range (I);
      Trk_DB (I).Az := Float (I) + Float (H);
      Logger.Log ("update");
   end Update;

   procedure Delete_Track (I : Integer) is
   begin
      Trk_DB (I).Id := 0;
      Logger.Log ("delete");
   end Delete_Track;
end Tracks;
""", encoding="utf-8")
    (src / "logger.ads").write_text("""package Logger is
   -- Writes a line to the log.
   -- Req: SRS-0103
   procedure Log (S : String);
   function Next_Id return Integer;
end Logger;
""", encoding="utf-8")
    (src / "logger.adb").write_text("""with Ada.Text_IO;
package body Logger is
   Counter : Integer := 0;
   procedure Log (S : String) is
   begin
      Ada.Text_IO.Put_Line (S);
   end Log;
   function Next_Id return Integer is
   begin
      Counter := Counter + 1;
      return Counter;
   end Next_Id;
end Logger;
""", encoding="utf-8")
    tests = ROOT / "ada" / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_tracks.adb").write_text("""with Tracks;
procedure Test_Tracks is
   -- Req: SRS-0004
begin
   Tracks.Delete_Track (1);
end Test_Tracks;
""", encoding="utf-8")
    (ROOT / "ada" / "samples.gpr").write_text("""-- The GPR project of the samples ([sources.code] project in the tests).
project Samples is
   type Build_Type is ("debug", "release");
   Build : Build_Type := external ("BUILD", "debug");

   for Source_Dirs use ("src", "tests");
   for Object_Dir use "obj/" & Build;

   package Compiler is
      case Build is
         when "debug" =>
            for Default_Switches ("Ada") use ("-g", "-gnata");
         when others =>
            for Default_Switches ("Ada") use ("-O2");
      end case;
   end Compiler;
end Samples;
""", encoding="utf-8")
    print("created", ROOT / "ada")


def doors() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT / "doors.sqlite"
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE requirements (
            id TEXT PRIMARY KEY, module TEXT, text TEXT, parent TEXT, status TEXT,
            verification TEXT, updated_at TEXT);
        CREATE TABLE links (source_id TEXT, source_module TEXT, target_id TEXT,
            target_module TEXT, link_type TEXT);
    """)
    random.seed(2)
    rows, links = [], []
    for n in range(1, 13):                     # system level: SSS
        rows.append((f"SSS-{n:04d}", "SSS_MISSION", f"The mission system shall provide capability {n}.",
                     None, "Approved", "Test", "2026-10-01"))
    for n in range(1, 41):                     # software level: SRS, each one satisfies an SSS
        parent = f"SSS-{(n % 12) + 1:04d}"
        ref = f" This refines SRS-{max(1, n - 1):04d}." if n % 5 == 0 else ""
        # lower-case IDs on purpose: the convention normalises them (id_normalize = "upper")
        rows.append((f"srs-{n:04d}", "SRS_TMS", f"The TMF shall process track event {n} within {random.randint(10, 500)} ms.{ref}",
                     None if n % 7 else f"SRS-{n - 1:04d}", random.choice(["Approved", "Draft"]),
                     random.choice(["Test", "Analysis", "Inspection"]), "2026-10-05"))
        links.append((f"SRS-{n:04d}", "SRS_TMS", parent, "SSS_MISSION", "satisfies"))
    rows += [(f"SRS-{n:04d}", "SRS_TMS_OLD", "legacy requirement", None, "Obsolete", "None", "2025-01-01")
             for n in (101, 102)]
    db.executemany("INSERT INTO requirements VALUES (?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO links VALUES (?,?,?,?,?)", links)
    db.commit()
    db.close()
    print("created", path, len(rows), "requirements,", len(links), "links")


def exchange() -> None:
    """The requirements of doors() in the exchange database, as a filler would leave them:
    each module under a heading object, attributes, hierarchy and typed links."""
    import sys
    sys.path.insert(0, str(ROOT.parents[1]))
    from atlas.doors.exchange import ExchangeWriter
    source = sqlite3.connect(ROOT / "doors.sqlite")
    source.row_factory = sqlite3.Row
    path = ROOT / "requirements.sqlite"
    path.unlink(missing_ok=True)
    with ExchangeWriter(path, filler="examples/make_samples.py") as w:
        for module, heading in (("SSS_MISSION", "Mission capabilities"), ("SRS_TMS", "Track management"),
                                ("SRS_TMS_OLD", "Obsolete requirements")):
            prefix = module.split("_")[0]
            top = f"{prefix}-{'0900' if module != 'SRS_TMS_OLD' else '0901'}"
            reqs = [{"id": top, "heading": heading, "kind": "heading", "level": 1, "outline": "1", "source_key": "0"}]
            for n, r in enumerate(source.execute("SELECT * FROM requirements WHERE module = ? ORDER BY id",
                                                 (module,)), 1):
                rid = r["id"].upper()
                reqs.append({"id": rid, "text": r["text"], "parent": (r["parent"] or top).upper(),
                             "level": 3 if r["parent"] else 2,
                             "outline": f"1.{n}", "source_key": str(n),
                             "attributes": {"status": r["status"], "verification": r["verification"]}})
            links = [{"source": l["source_id"], "target": l["target_id"], "type": l["link_type"],
                      "target_module": l["target_module"]}
                     for l in source.execute("SELECT * FROM links WHERE source_module = ?", (module,))]
            w.replace_module(module, reqs, links, source=f"/Mission/{module}")
    source.close()
    print("created", path)


def reqif(path: Path | None = None) -> Path:
    """The modules of requirements.sqlite as one ReqIF file: a SPECIFICATION per module, the
    hierarchy of the parents, the text as XHTML, Status as an enumeration and the links as
    SPEC-RELATIONs, typed by their name. The headings are of their own SPEC-OBJECT-TYPE,
    without Status and Verification Method."""
    import json
    import xml.etree.ElementTree as ET
    ns = "http://www.omg.org/spec/ReqIF/20110401/reqif.xsd"
    xhtml = "http://www.w3.org/1999/xhtml"
    ET.register_namespace("", ns)
    ET.register_namespace("xhtml", xhtml)
    q = lambda tag: f"{{{ns}}}{tag}"

    def el(parent, tag, text=None, **attrs):
        e = ET.SubElement(parent, q(tag), {k.replace("_", "-"): v for k, v in attrs.items()})
        if text is not None:
            e.text = text
        return e

    def ref(parent, holder, kind, target):
        el(el(parent, holder), f"{kind}-REF", target)

    source = sqlite3.connect(ROOT / "requirements.sqlite")
    source.row_factory = sqlite3.Row
    rows = source.execute("SELECT * FROM exchange_requirements ORDER BY module, position").fetchall()
    links = source.execute("SELECT * FROM exchange_links ORDER BY module, source, target").fetchall()
    modules = [r[0] for r in source.execute("SELECT module FROM exchange_modules ORDER BY module")]
    source.close()
    statuses = sorted({json.loads(r["attributes"]).get("status") for r in rows} - {None})

    root = ET.Element(q("REQ-IF"))
    header = el(el(root, "THE-HEADER"), "REQ-IF-HEADER", IDENTIFIER="header")
    for tag, text in (("CREATION-TIME", "2026-10-17T00:00:00Z"), ("REQ-IF-TOOL-ID", "examples/make_samples.py"),
                      ("REQ-IF-VERSION", "1.0"), ("SOURCE-TOOL-ID", "examples/make_samples.py"),
                      ("TITLE", "AtlasLM samples")):
        el(header, tag, text)
    content = el(el(root, "CORE-CONTENT"), "REQ-IF-CONTENT")
    types = el(content, "DATATYPES")
    el(types, "DATATYPE-DEFINITION-STRING", IDENTIFIER="dt-string", LONG_NAME="String", MAX_LENGTH="10000")
    el(types, "DATATYPE-DEFINITION-XHTML", IDENTIFIER="dt-xhtml", LONG_NAME="XHTML")
    values = el(el(types, "DATATYPE-DEFINITION-ENUMERATION", IDENTIFIER="dt-status", LONG_NAME="Status"),
                "SPECIFIED-VALUES")
    for n, status in enumerate(statuses):
        el(el(el(values, "ENUM-VALUE", IDENTIFIER=f"status-{status.lower()}", LONG_NAME=status), "PROPERTIES"),
           "EMBEDDED-VALUE", KEY=str(n), OTHER_CONTENT="")
    spec_types = el(content, "SPEC-TYPES")
    for ident, name, attributes in (("type-heading", "Heading", ("id", "heading")),
                                    ("type-requirement", "Requirement", ("id", "text", "status", "verification"))):
        defined = el(el(spec_types, "SPEC-OBJECT-TYPE", IDENTIFIER=ident, LONG_NAME=name), "SPEC-ATTRIBUTES")
        for a in attributes:
            kind, long_name, datatype = {"id": ("STRING", "Requirement ID", "dt-string"),
                                         "heading": ("STRING", "ReqIF.ChapterName", "dt-string"),
                                         "text": ("XHTML", "ReqIF.Text", "dt-xhtml"),
                                         "status": ("ENUMERATION", "Status", "dt-status"),
                                         "verification": ("STRING", "Verification Method", "dt-string")}[a]
            d = el(defined, f"ATTRIBUTE-DEFINITION-{kind}", IDENTIFIER=f"{ident}-{a}", LONG_NAME=long_name,
                   **({"MULTI_VALUED": "false"} if kind == "ENUMERATION" else {}))
            ref(d, "TYPE", f"DATATYPE-DEFINITION-{kind}", datatype)
    for kind in sorted({l["type"] for l in links}):
        el(spec_types, "SPEC-RELATION-TYPE", IDENTIFIER=f"rel-{kind}", LONG_NAME=kind)
    el(spec_types, "SPECIFICATION-TYPE", IDENTIFIER="type-module", LONG_NAME="Module")

    objects = el(content, "SPEC-OBJECTS")
    key = lambda rid: f"obj-{rid}"
    for r in rows:
        kind = "heading" if r["kind"] == "heading" else "requirement"
        o = el(objects, "SPEC-OBJECT", IDENTIFIER=key(r["id"]), LAST_CHANGE="2026-10-17T00:00:00Z")
        ref(o, "TYPE", "SPEC-OBJECT-TYPE", f"type-{kind}")
        vals = el(o, "VALUES")
        string = lambda attribute, value, vals=vals, kind=kind: ref(
            el(vals, "ATTRIBUTE-VALUE-STRING", THE_VALUE=value), "DEFINITION", "ATTRIBUTE-DEFINITION-STRING",
            f"type-{kind}-{attribute}")
        string("id", r["id"])
        if kind == "heading":
            string("heading", r["heading"])
            continue
        v = el(vals, "ATTRIBUTE-VALUE-XHTML")
        ref(v, "DEFINITION", "ATTRIBUTE-DEFINITION-XHTML", f"type-{kind}-text")
        ET.SubElement(el(v, "THE-VALUE"), f"{{{xhtml}}}div").text = r["text"]
        attributes = json.loads(r["attributes"])
        v = el(vals, "ATTRIBUTE-VALUE-ENUMERATION")
        ref(v, "DEFINITION", "ATTRIBUTE-DEFINITION-ENUMERATION", f"type-{kind}-status")
        el(el(v, "VALUES"), "ENUM-VALUE-REF", f"status-{attributes['status'].lower()}")
        string("verification", attributes["verification"])
    relations = el(content, "SPEC-RELATIONS")
    for n, l in enumerate(links, 1):
        r = el(relations, "SPEC-RELATION", IDENTIFIER=f"link-{n}", LAST_CHANGE="2026-10-17T00:00:00Z")
        ref(r, "TYPE", "SPEC-RELATION-TYPE", f"rel-{l['type']}")
        ref(r, "SOURCE", "SPEC-OBJECT", key(l["source"]))
        ref(r, "TARGET", "SPEC-OBJECT", key(l["target"]))
    specifications = el(content, "SPECIFICATIONS")
    for module in modules:
        spec = el(specifications, "SPECIFICATION", IDENTIFIER=f"spec-{module}", LONG_NAME=module,
                  LAST_CHANGE="2026-10-17T00:00:00Z")
        ref(spec, "TYPE", "SPECIFICATION-TYPE", "type-module")
        holders = {None: el(spec, "CHILDREN")}
        for r in (r for r in rows if r["module"] == module):       # in order: a parent comes before its children
            h = el(holders[r["parent"]], "SPEC-HIERARCHY", IDENTIFIER=f"h-{r['id']}",
                   LAST_CHANGE="2026-10-17T00:00:00Z")
            ref(h, "OBJECT", "SPEC-OBJECT", key(r["id"]))
            holders[r["id"]] = el(h, "CHILDREN")
        for holder in holders.values():                             # no empty CHILDREN
            for h in list(holder):
                children = h.find(q("CHILDREN"))
                if children is not None and not len(children):
                    h.remove(children)
    path = path or ROOT / "requirements.reqif"
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    print("created", path)
    return path


if __name__ == "__main__":
    ada()
    doors()
    exchange()
    reqif()
    try:
        pdfs()
    except ImportError:
        print("pymupdf not installed: PDFs not generated (pip install pymupdf)")
