import sys
from pathlib import Path

import pytest

from atlas.code import AdaAnalyzer, extract_requirements, requirement_pattern

pytest.importorskip("tree_sitter_ada")

SOURCE = b"""with Ada.Text_IO;
with Store;
package body Radar is
   -------------------------------------------
   -- Smooths the plots of a scan.
   -- REQ : RDR-0010, RDR_0011 also see the ICD
   -------------------------------------------
   procedure Smooth (Plots : in out Plot_Array; N : Integer) is
      Window : Float_Array (1 .. 5);
      function Weight (I : Integer) return Float is
      begin
         return Kernel (I);           -- a call of the nested function, not of Smooth
      end Weight;
   begin
      Window (1) := Weight (N);       -- Window (1) is indexing: a local array
      Plots (N) := Store.Last;        -- Plots (N) is indexing: a parameter
      Ada.Text_IO.Put_Line ("done");  -- Req: RDR-0012
   end Smooth;
end Radar;
"""


def test_requirement_tags():
    assert extract_requirements("-- Req: A-B, C-D deletes messages", requirement_pattern({})) == ["A-B", "C-D"]
    assert extract_requirements("-- @req SYS-1\n-- @req SYS-2", requirement_pattern({"req_tag": "@req"})) == \
        ["SYS-1", "SYS-2"]
    assert extract_requirements("-- [[TRACE SRS-9]]", requirement_pattern({"req_regex": r"\[\[TRACE ([^\]]+)\]\]"})) == \
        ["SRS-9"]
    assert extract_requirements("-- Req: none here", requirement_pattern({})) == []


def test_requirement_ids_of_the_convention():
    """The probe of the analysis of 2026-10: without the expression of an ID, a tag is read
    token by token and loses what follows a word or a slash."""
    import re
    from atlas.doors import Canonical, id_regex, id_shapes, normalize_id
    tag = requirement_pattern({})
    assert extract_requirements("-- Req: SRS-0004 and SRS-0005", tag) == ["SRS-0004"]       # as before
    ids = re.compile(id_regex({"id_pattern": "^(?:SRS|SSS)-\\d{4}$"}))   # id_pattern without anchors
    assert extract_requirements("-- Req: SRS-0004 and SRS-0005", tag, ids) == ["SRS-0004", "SRS-0005"]
    assert extract_requirements("-- Req: see SRS-0004", tag, ids) == ["SRS-0004"]
    assert extract_requirements("-- Req: SRS-0004 / SRS-0005", tag, ids) == ["SRS-0004", "SRS-0005"]
    assert extract_requirements("-- Req: srs-0004, SRS-00045, XSRS-0006", tag, ids) == ["srs-0004"]
    assert extract_requirements("-- SRS-0007 without a tag", tag, ids) == []
    assert id_regex({"id_regex": r"\bR\d+\b", "id_pattern": "^X$"}) == r"\bR\d+\b" and id_regex({}) == ""
    assert re.compile(id_regex({"id_pattern": "(?i)^srs-\\d+$"})).findall("Req: SRS-1, srs-2") == ["SRS-1", "srs-2"]

    # "SRS 0004" and "srs-4": a looser id_regex finds them and id_canonical makes them SRS-0004
    loose = re.compile(r"(?i)\b(?:SRS|SSS)[-_ ]?\d+\b")
    canonical = Canonical({"regex": r"^(SRS|SSS)[-_ ]?0*(\d+)$", "format": "{1}-{2:04d}"})
    found = extract_requirements("-- Req: SRS 0004, srs-4, SSS_12", tag, loose)
    assert {normalize_id(r) for r in found} == {"SRS0004", "SRS-4", "SSS_12"}       # three nodes before
    assert {normalize_id(r, "upper", canonical) for r in found} == {"SRS-0004", "SSS-0012"}
    assert normalize_id("SRS - 0004", "upper", canonical) == "SRS-0004"
    assert normalize_id("srs-4", "none", canonical) == "srs-4" and normalize_id("TBD", "upper", canonical) == "TBD"
    for bad in ({"regex": "^(SRS)$"}, {"regex": "^(SRS)$", "format": "{2}"}, {"regex": "(", "format": "x"},
                {"regex": "x", "format": "x", "case": "upper"}):
        with pytest.raises(ValueError, match="id_canonical"):
            Canonical(bad)
    assert id_shapes(["SRS-0004", "srs-4", "SRS 0004", "SRS-0005", "TBD"]) == [
        {"prefix": "SRS", "separator": "-", "digits": 4, "count": 2, "example": "SRS-0004"},
        {"prefix": "SRS", "separator": " ", "digits": 4, "count": 1, "example": "SRS 0004"},
        {"prefix": "other", "separator": "", "digits": 0, "count": 1, "example": "TBD"},
        {"prefix": "srs", "separator": "-", "digits": 1, "count": 1, "example": "srs-4"}]

    # the analyzers take it in their configuration (code.parse puts it there)
    src = b"package body P is\n   -- Req: SRS-0004 and SRS-0005\n   procedure Run is\n   begin\n      null;" \
          b"  -- Req: see SRS-0006 / SRS-0007\n   end Run;\nend P;\n"
    run = AdaAnalyzer({"id_regex": ids.pattern}).analyze_file(Path("p.adb"), src).subprograms[0]
    assert run["reqs"] == ["SRS-0004", "SRS-0005", "SRS-0006", "SRS-0007"]
    assert AdaAnalyzer({}).analyze_file(Path("p.adb"), src).subprograms[0]["reqs"] == ["SRS-0004"]
    from atlas.code import PythonAnalyzer
    py = b"def run():\n    # Req: SRS-0004 and SRS-0005\n    pass\n"
    assert PythonAnalyzer({"id_regex": ids.pattern}).analyze_file(Path("p.py"), py).subprograms[0]["reqs"] == \
        ["SRS-0004", "SRS-0005"]


def test_ada_bodies_calls_comments_and_requirements(tmp_path):
    f = tmp_path / "radar-filters.adb"
    unit = AdaAnalyzer({}).analyze_file(f, SOURCE)
    assert unit.unit == "radar.filters" and unit.imports == ["ada.text_io", "store"]
    subs = {s["name"]: s for s in unit.subprograms}
    smooth = subs["smooth"]
    assert smooth["calls"] == ["ada.text_io.put_line", "weight"]          # not window, plots or kernel
    assert smooth["doc"] == "Smooths the plots of a scan.\nREQ : RDR-0010, RDR_0011 also see the ICD"
    assert smooth["reqs"] == ["RDR-0010", "RDR-0012", "RDR_0011"]
    assert subs["weight"]["calls"] == ["kernel"]
    assert (smooth["start_line"], smooth["end_line"]) == (8, 18)


# The probe of the analysis of 2026-10 (section 6.2): what ada-1 lost or mixed up
PROBE = b"""package body Probe is
   type Count is range 0 .. 9;
   -- The main loop.
   -- Req: SRS-0010
   task body Main_Loop is
      Ticks : Count := 0;
   begin
      loop
         Run (1);                       -- Req: SRS-0011
         Put_Line (Count'Image (Ticks));
      end loop;
   end Main_Loop;
   protected body Lock is
      -- Req: SRS-0012
      entry Seize when not Busy is
      begin
         Busy := True;
         Notify;
      end Seize;
      procedure Release is
      begin
         Busy := False;
      end Release;
   end Lock;
   function "+" (A, B : Vector) return Vector is
   begin
      return Add (A, B);                -- Req: SRS-0013
   end "+";
   -- Req: SRS-0014
   procedure Run (N : Integer) is
      T : Count := 3;
   begin
      Put_Line (T'Image);
      Put_Line (Count'Image (T));
      T'Write (Stream, T);
      Log (N);
   end Run;
   procedure Run is
   begin
      Reset;                            -- Req: SRS-0015
   end Run;
end Probe;
"""


def test_ada_tasks_entries_protected_bodies_operators_and_attributes():
    unit = AdaAnalyzer({}).analyze_file(Path("probe.adb"), PROBE)
    subs = [(s["name"], s["kind"], s["start_line"], s["end_line"], s["calls"], s["reqs"]) for s in unit.subprograms]
    assert subs == [
        ("main_loop", "task", 5, 12, ["put_line", "run"], ["SRS-0010", "SRS-0011"]),     # not count: an attribute
        ("lock", "protected", 13, 24, [], []),                   # the tags of its entries are theirs
        ("seize", "entry", 15, 19, ["notify"], ["SRS-0012"]),
        ("release", "procedure", 20, 23, [], []),
        ('"+"', "operator", 25, 28, ["add"], ["SRS-0013"]),
        ("run", "procedure", 30, 37, ["log", "put_line"], ["SRS-0014"]),   # not t (T'Image, T'Write) nor count
        ("run", "procedure", 38, 41, ["reset"], ["SRS-0015"])]
    assert unit.variables == []                                   # Ticks is local to the task
    spec = AdaAnalyzer({}).analyze_file(Path("probe.ads"), b'package Probe is\n   -- Adds.\n'
                                        b'   function "+" (A, B : Vector) return Vector;\n   procedure Run;\nend Probe;\n')
    assert [(d["name"], d["kind"], d["doc"]) for d in spec.declarations] == [('"+"', "operator", "Adds."),
                                                                            ("run", "procedure", "")]
    assert AdaAnalyzer({}).version() == "ada-3"


def test_ada_use_clauses_and_aliases():
    src = b"""with Ada.Text_IO; use Ada.Text_IO;
package body P is
   package TIO renames Ada.Text_IO;
   use type Store.Count;
   use all type Store.Kind;
   use Store, Other.Pkg;
   procedure Q (X : Integer) renames Store.Put;
   function "-" (A, B : T) return T renames Sub;
   package Int_Stack is new Stacks (Integer);
   procedure P2 is new Gen_P;
   procedure Run is
      use Inner;
   begin
      Q (1);
   end Run;
end P;
"""
    unit = AdaAnalyzer({}).analyze_file(Path("p.adb"), src)
    assert unit.uses == ["ada.text_io", "store", "other.pkg", "inner"]        # not the `use type` ones
    assert unit.aliases == {"tio": "ada.text_io", "q": "store.put", '"-"': "sub", "int_stack": "stacks",
                            "p2": "gen_p"}
    assert unit.subprograms[0]["calls"] == ["q"]                  # the graph resolves the alias


def test_spec_declarations_types_and_variables(samples):
    f = samples / "ada" / "src" / "tracks.ads"
    unit = AdaAnalyzer({"req_tag": "Req:"}).analyze_file(f, f.read_bytes())
    assert [(d["name"], d["reqs"]) for d in unit.declarations] == \
        [("update", ["SRS-0002", "SRS-0003"]), ("delete_track", ["SRS-0004"])]
    track = next(t for t in unit.types if t["name"] == "track")
    assert track["record"] and track["reqs"] == ["SRS-0001"] and "Az   : Float;" in track["source"]
    assert {(v["name"], v["constant"]) for v in unit.variables} == {("trk_db", False), ("max_tracks", True)}


LEGACY = Path(__file__).resolve().parents[1] / "legacy"


@pytest.mark.skipif(not (LEGACY / "ada_graph.py").exists(), reason="legacy/ has been removed")
def test_same_analysis_as_ada_graph(samples, tmp_path):
    """The analysis of every sample file is the one of legacy/ada_graph.analizar, which
    lists types in no stable order and has no `kind` (to be deleted together with legacy/)."""
    sys.path.insert(0, str(LEGACY))
    try:
        import ada_graph
    finally:
        sys.path.remove(str(LEGACY))
    from atlas.code.ada import analyze_ada
    pattern = requirement_pattern({})
    files = sorted((samples / "ada").rglob("*.ad?")) + [tmp_path / "radar.adb"]
    files[-1].write_bytes(SOURCE)
    for f in files:
        old = ada_graph.analizar(f, pattern)
        new = analyze_ada(f, f.read_bytes(), pattern)
        in_order = lambda items: sorted(items, key=lambda x: (x.get("line") or x["start_line"], x["name"]))
        assert new["withs"] == old["withs"] and new["unit"] == old["unit"], f
        without_kind = lambda items: [{k: v for k, v in x.items() if k != "kind"} for x in items]
        assert without_kind(new["declarations"]) == in_order(old["decls"]), f
        assert without_kind(new["subprograms"]) == in_order(old["subprogramas"]), f
        assert new["types"] == in_order(old["tipos"]) and new["variables"] == in_order(old["variables"]), f
