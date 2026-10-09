#!/usr/bin/env python3
"""Genera datos de ejemplo en examples/samples/ para desarrollar y probar sin acceso a
documentos, código ni DOORS reales:

  samples/docs/spec_con_marcadores.pdf   especificación con índice navegable (marcadores)
  samples/docs/spec_sin_marcadores.pdf   la misma, sin marcadores y con números separados
  samples/docs/manual_hmi.md             un Markdown con requisitos
  samples/ada/src/*.ads, *.adb           proyecto Ada pequeño con comentarios Req:
  samples/doors.sqlite                   explotación de DOORS: módulos, requisitos, enlaces

Requiere: pip install pymupdf
"""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

RAIZ = Path(__file__).resolve().parent / "samples"


def pdfs() -> None:
    import pymupdf
    random.seed(1)
    (RAIZ / "docs").mkdir(parents=True, exist_ok=True)

    def build(nombre: str, con_marcadores: bool) -> None:
        doc = pymupdf.open()
        estado = {"page": None, "y": 0, "n": 0}
        toc = []

        def nueva_pagina():
            estado["page"] = doc.new_page(width=595, height=842)
            estado["n"] += 1
            estado["page"].insert_text((72, 40), "EMPRESA - Company Confidential", fontsize=8)
            estado["page"].insert_text((72, 52), "Doc. TMS-SRS-001  Issue 2", fontsize=8)
            estado["page"].insert_text((280, 815), f"Page {estado['n']} of 99", fontsize=8)
            estado["y"] = 90

        def linea(texto, x=72, size=10, gap=14):
            if estado["y"] > 780:
                nueva_pagina()
            estado["page"].insert_text((x, estado["y"]), texto, fontsize=size)
            estado["y"] += gap

        nueva_pagina()
        linea("Track Management Software Requirements Specification", size=16, gap=30)
        linea("Table of Contents", size=12, gap=20)
        for c in range(1, 4):
            linea(f"{c} Chapter {c} title ........................ {c * 3}")
        nueva_pagina()
        n = 0
        for c in range(1, 4):
            linea(f"{c}", size=14, gap=0); linea(f"Chapter {c} Functions", x=110, size=14, gap=24)
            toc.append([1, f"{c} Chapter {c} Functions", estado["n"]])
            for s1 in range(1, 4):
                linea(f"{c}.{s1}", size=12, gap=0); linea(f"Function group {c}.{s1}", x=130, size=12, gap=20)
                toc.append([2, f"{c}.{s1} Function group {c}.{s1}", estado["n"]])
                for s2 in range(1, 4):
                    num = f"{c}.{s1}.{s2}"
                    linea(num, size=11, gap=0); linea(f"Track handling {num}", x=150, size=11, gap=18)
                    for r in range(random.randint(4, 8)):
                        n += 1
                        linea(f"Req: SRS-{n:04d} The TMF shall process track event {r} of")
                        linea(f"group {num} within {random.randint(10, 500)} ms after reception (see SSS-{(n % 12) + 1:04d}).", gap=16)
        if con_marcadores:
            doc.set_toc(toc)
        doc.save(RAIZ / "docs" / nombre)
        print("creado", RAIZ / "docs" / nombre, estado["n"], "páginas")

    build("spec_con_marcadores.pdf", True)
    build("spec_sin_marcadores.pdf", False)
    (RAIZ / "docs" / "manual_hmi.md").write_text(
        "# Manual HMI\n\n1 General\n\nReq: SRS-0101 The HMI shall display tracks.\n\n"
        "2 Alarms\n\nReq: SRS-0102 The HMI shall raise an alarm when a track is lost. Alarm latency is TBD.\n\n"
        "3 Logging\n\nReq: SRS-0103 The HMI shall keep logs for 30 days.\n", encoding="utf-8")


def ada() -> None:
    src = RAIZ / "ada" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "tracks.ads").write_text("""package Tracks is
   -- Registro de una traza radar.
   -- Req: SRS-0001
   type Track is record
      Id   : Integer;
      Az   : Float;
   end record;
   type Track_Array is array (1 .. 100) of Track;
   subtype Hour_Range is Integer range 0 .. 23;
   Trk_DB : Track_Array;
   Max_Tracks : constant Integer := 100;
   -- Actualiza la traza I.
   -- Req: SRS-0002, SRS-0003
   procedure Update (I : Integer);
   -- Elimina la traza I de la base de datos.
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
   -- Escribe una línea en el log.
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
    tests = RAIZ / "ada" / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_tracks.adb").write_text("""with Tracks;
procedure Test_Tracks is
   -- Req: SRS-0004
begin
   Tracks.Delete_Track (1);
end Test_Tracks;
""", encoding="utf-8")
    print("creado", RAIZ / "ada")


def doors() -> None:
    RAIZ.mkdir(parents=True, exist_ok=True)
    path = RAIZ / "doors.sqlite"
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
    filas, enlaces = [], []
    for n in range(1, 13):                     # sistema: SSS
        filas.append((f"SSS-{n:04d}", "SSS_MISSION", f"The mission system shall provide capability {n}.",
                      None, "Approved", "Test", "2026-10-01"))
    for n in range(1, 41):                     # software: SRS, cada uno satisface un SSS
        padre = f"SSS-{(n % 12) + 1:04d}"
        ref = f" This refines SRS-{max(1, n - 1):04d}." if n % 5 == 0 else ""
        filas.append((f"srs-{n:04d}", "SRS_TMS", f"The TMF shall process track event {n} within {random.randint(10, 500)} ms.{ref}",
                      None if n % 7 else f"SRS-{n - 1:04d}", random.choice(["Approved", "Draft"]),
                      random.choice(["Test", "Analysis", "Inspection"]), "2026-10-05"))
        enlaces.append((f"SRS-{n:04d}", "SRS_TMS", padre, "SSS_MISSION", "satisfies"))
    filas += [(f"SRS-{n:04d}", "SRS_TMS_OLD", "legacy requirement", None, "Obsolete", "None", "2025-01-01")
              for n in (101, 102)]
    db.executemany("INSERT INTO requirements VALUES (?,?,?,?,?,?,?)", filas)
    db.executemany("INSERT INTO links VALUES (?,?,?,?,?)", enlaces)
    db.commit()
    db.close()
    print("creado", path, len(filas), "requisitos,", len(enlaces), "enlaces")


if __name__ == "__main__":
    ada()
    doors()
    try:
        pdfs()
    except ImportError:
        print("pymupdf no instalado: no se generan los PDF (pip install pymupdf)")
