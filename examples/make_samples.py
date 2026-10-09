#!/usr/bin/env python3
"""Generates sample data in examples/samples/ to develop and test without access to real
documents, code or DOORS:

  samples/docs/spec_with_bookmarks.pdf      specification with a navigable outline (bookmarks)
  samples/docs/spec_without_bookmarks.pdf   the same one, without bookmarks and with split numbers
  samples/docs/hmi_manual.md                a Markdown file with requirements
  samples/ada/src/*.ads, *.adb              small Ada project with Req: comments
  samples/doors.sqlite                      DOORS operational copy: modules, requirements, links

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


if __name__ == "__main__":
    ada()
    doors()
    try:
        pdfs()
    except ImportError:
        print("pymupdf not installed: PDFs not generated (pip install pymupdf)")
