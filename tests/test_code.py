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
        assert new["subprograms"] == in_order(old["subprogramas"]), f
        assert new["types"] == in_order(old["tipos"]) and new["variables"] == in_order(old["variables"]), f
