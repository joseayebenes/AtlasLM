#!/usr/bin/env python3
"""Generates sample data in examples/samples/ to develop and test without access to real
documents, code or DOORS:

  samples/docs/spec_with_bookmarks.pdf      specification with a navigable outline (bookmarks)
  samples/docs/spec_without_bookmarks.pdf   the same one, without bookmarks and with split numbers
  samples/docs/hmi_manual.md                a Markdown file with requirements
  samples/scanned/scanned_page.pdf          a page that is only an image (a scan without OCR)
  samples/tables/interface_tables.pdf       requirements in a table with borders, a caption, a list
                                            and numbered steps
  samples/ada/src/*.ads, *.adb              small Ada project with Req: comments
  samples/doors.sqlite                      DOORS operational copy: modules, requirements, links
  samples/requirements.sqlite               the same requirements in the exchange database of atlas
                                            (atlas.doors.exchange), with headings and hierarchy

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


def scanned() -> None:
    """A one-page PDF whose page is an image of text, as a scanner leaves it: no text to
    extract. Outside samples/docs, so that the runs over the samples do not see it."""
    import pymupdf
    (ROOT / "scanned").mkdir(parents=True, exist_ok=True)
    text = pymupdf.open()
    page = text.new_page(width=595, height=842)
    page.insert_text((72, 90), "Scanned Interface Specification", fontsize=16)
    page.insert_text((72, 130), "Req: SRS-0201 The TMF shall accept scanned input.", fontsize=11)
    pix = page.get_pixmap(dpi=100)
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=pix)
    doc.save(ROOT / "scanned" / "scanned_page.pdf")
    print("created", ROOT / "scanned" / "scanned_page.pdf")


def tables() -> None:
    """A PDF whose requirements are the rows of a table with borders (ID | Requirement |
    Verification), one of them in two lines, with its caption above, and a list and
    numbered steps around it. Outside samples/docs, like scanned()."""
    import pymupdf
    (ROOT / "tables").mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 60), "Interface Control Document", fontsize=16)
    page.insert_text((72, 90), "Doc. TMS-ICD-002  Issue 1", fontsize=8)
    page.insert_text((72, 130), "1 Interface requirements", fontsize=13)
    y = 155
    for line in ("The interface carries:", "- track updates", "- alarms"):
        page.insert_text((72, y), line, fontsize=10)
        y += 14
    page.insert_text((72, y + 10), "Table 1 - Interface requirements", fontsize=10)
    rows = [("ID", "Requirement", "Verification"),
            ("ICD-0001", ["The TMF shall send track updates", "every 500 ms."], "Test"),
            ("ICD-0002", ["The TMF shall raise an alarm within 2 s."], "Analysis"),
            ("ICD-0003", ["The TMF shall log every message."], "Inspection")]
    xs, top = [72, 160, 430, 523], y + 22
    for row in rows:
        lines = row[1] if isinstance(row[1], list) else [row[1]]
        height = 8 + 13 * len(lines)
        for k in range(3):
            page.draw_rect(pymupdf.Rect(xs[k], top, xs[k + 1], top + height), color=(0, 0, 0), width=0.7)
        page.insert_text((xs[0] + 4, top + 14), row[0], fontsize=10)
        for n, text in enumerate(lines):
            page.insert_text((xs[1] + 4, top + 14 + 13 * n), text, fontsize=10)
        page.insert_text((xs[2] + 4, top + 14), row[2], fontsize=10)
        top += height
    y = top + 30
    for line in ("2 Start-up", "To start the interface:", "1. Power the unit on.", "2. Wait for the green light.",
                 "3. Send the first track update."):
        page.insert_text((72, y), line, fontsize=13 if line[0] == "2" and "Start" in line else 10)
        y += 14 if "Start" not in line else 22
    doc.save(ROOT / "tables" / "interface_tables.pdf")
    print("created", ROOT / "tables" / "interface_tables.pdf")


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


if __name__ == "__main__":
    ada()
    doors()
    exchange()
    try:
        pdfs()
        scanned()
        tables()
    except ImportError:
        print("pymupdf not installed: PDFs not generated (pip install pymupdf)")
