import sys
from pathlib import Path

import pytest

from atlas.code import AdaAnalyzer, extract_requirements, requirement_pattern
from atlas.plugins import CodeUnit, call_name

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


def test_ada_bodies_calls_comments_and_requirements(tmp_path):
    f = tmp_path / "radar-filters.adb"
    unit = AdaAnalyzer({}).analyze_file(f, SOURCE)
    assert unit.unit == "radar.filters" and unit.imports == ["ada.text_io", "store"]
    subs = {s["name"]: s for s in unit.subprograms}
    smooth = subs["smooth"]
    assert smooth["calls"] == [{"name": "weight", "line": 15, "column": 21},           # not window, plots or kernel
                               {"name": "ada.text_io.put_line", "line": 17, "column": 7}]
    assert smooth["doc"] == "Smooths the plots of a scan.\nREQ : RDR-0010, RDR_0011 also see the ICD"
    assert smooth["reqs"] == ["RDR-0010", "RDR-0012", "RDR_0011"]
    assert subs["weight"]["calls"] == [{"name": "kernel", "line": 12, "column": 17}]
    assert (smooth["start_line"], smooth["end_line"]) == (8, 18)
    assert smooth["kind"] == "procedure" and subs["weight"]["kind"] == "function"
    assert smooth["signature"] == "procedure Smooth (Plots : in out Plot_Array; N : Integer)"
    assert subs["weight"]["signature"] == "function Weight (I : Integer) return Float"
    assert AdaAnalyzer.predefined >= {"integer", "float", "string"}


def test_the_column_of_a_call_counts_characters_and_old_units_still_load(tmp_path):
    src = "package body P is\n   procedure Go is\n   begin\n      Ñu (1); Q.Run;\n   end Go;\nend P;\n"
    unit = AdaAnalyzer({}).analyze_file(tmp_path / "p.adb", src.encode("utf-8"))
    assert unit.subprograms[0]["calls"] == [{"name": "ñu", "line": 4, "column": 7},
                                            {"name": "q.run", "line": 4, "column": 15}]   # Ñ is two bytes
    stored = {"file": "p.adb", "unit": "p", "language": "ada", "subprograms": [{"name": "go", "calls": ["q.run"]}],
              "tests": []}                                   # as 0.2 stored it: calls without place, and tests
    old = CodeUnit.from_dict(stored)
    assert [call_name(c) for c in old.subprograms[0]["calls"]] == ["q.run"] and not hasattr(old, "tests")


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
    lists types in no stable order (to be deleted together with legacy/)."""
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
        assert new["declarations"] == in_order(old["decls"]), f
        names = [{**{k: v for k, v in s.items() if k not in ("kind", "signature")},
                  "calls": sorted({call_name(c) for c in s["calls"]})} for s in new["subprograms"]]
        assert names == in_order(old["subprogramas"]), f
        assert new["types"] == in_order(old["tipos"]) and new["variables"] == in_order(old["variables"]), f
