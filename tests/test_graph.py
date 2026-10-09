import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_ada")
nx = pytest.importorskip("networkx")

from atlas.code import AdaAnalyzer, requirement_pattern  # noqa: E402
from atlas.graph import GraphStore, KnowledgeGraph, build_code_graph, export_graph  # noqa: E402

SAME_NAMES = {   # two packages with an Update and a Reset each
    "alpha.ads": "package Alpha is\n   -- Req: SYS-1\n   procedure Update;\n   procedure Spec_Only;\nend Alpha;\n",
    "alpha.adb": "package body Alpha is\n   procedure Update is\n   begin\n      Reset;\n   end Update;\n"
                 "   procedure Reset is\n   begin\n      null;\n   end Reset;\nend Alpha;\n",
    "beta.adb": "package body Beta is\n   procedure Update is\n   begin\n      Reset;\n   end Update;\n"
                "   procedure Reset is\n   begin\n      null;\n   end Reset;\nend Beta;\n",
    "gamma.adb": "with Beta;\npackage body Gamma is\n   procedure Run is\n   begin\n      Update;\n"
                 "      Alpha.Update;\n   end Run;\nend Gamma;\n",
    "omega.adb": "package body Omega is\n   procedure Go is\n   begin\n      Reset;\n   end Go;\nend Omega;\n",
}


def _units(files):
    a = AdaAnalyzer({})
    return [a.analyze_file(f, f.read_bytes()) for f in files]


@pytest.fixture
def sample_graph(samples):
    return build_code_graph(_units(sorted((samples / "ada").rglob("*.ad?"))))


def _edges(G, kind=None):
    return {(u, v) if kind else (u, v, d["kind"]) for u, v, d in G.edges(data=True) if not kind or d["kind"] == kind}


def test_sample_code_graph(sample_graph):
    G, stats = sample_graph
    assert _edges(G, "calls") == {("tracks.update", "logger.log"), ("tracks.delete_track", "logger.log"),
                                  ("logger.log", "ada.text_io.put_line"),
                                  ("test_tracks.test_tracks", "tracks.delete_track")}
    assert _edges(G, "converts") == {("tracks.update", "tracks.hour_range")}
    assert _edges(G, "uses") == {("tracks.update", "tracks.trk_db"), ("tracks.delete_track", "tracks.trk_db")}
    update = G.nodes["tracks.update"]
    assert update["doc"].startswith("Updates track I.") and update["requirements"] == ["SRS-0002", "SRS-0003"]
    assert update["spec_file"].endswith("tracks.ads") and update["file"].endswith("tracks.adb")
    assert G.nodes["test_tracks"]["kind"] == "unit" and G.nodes["test_tracks.test_tracks"]["kind"] == "subprogram"
    assert stats["edges_by_kind"] == {"calls": 4, "converts": 1, "uses": 2, "predefined": 1}
    assert stats["unresolved_calls"] == 1 and stats["top_requirements"][0] == ("SRS-0004", 2)


def test_same_names_in_two_packages_are_two_nodes(tmp_path):
    for name, text in SAME_NAMES.items():
        (tmp_path / name).write_text(text)
    G, stats = build_code_graph(_units(sorted(tmp_path.glob("*.ad?"))))
    calls = _edges(G, "calls")
    assert ("alpha.update", "alpha.reset") in calls and ("beta.update", "beta.reset") in calls   # own unit first
    assert ("gamma.run", "beta.update") in calls          # a bare name: the unit it imports with `with`
    assert ("gamma.run", "alpha.update") in calls         # a qualified name
    assert {("omega.go", "alpha.reset"), ("omega.go", "beta.reset")} <= calls   # ambiguous: both
    assert stats["ambiguous_calls"] == 1
    assert ("alpha", "alpha.spec_only") in _edges(G, "defines")           # declared without a body
    assert G.nodes["alpha.update"]["requirements"] == ["SYS-1"] and "beta.update" not in G.predecessors("SYS-1")
    g = KnowledgeGraph(G, root=tmp_path)
    assert [i["id"] for i in g.impact("alpha.reset")["items"]] == ["alpha.update", "omega.go", "gamma.run"]
    assert g.node("Update")["candidates"] == ["alpha.update", "beta.update"]     # by simple name: ambiguous
    assert g.node("Run")["id"] == "gamma.run"


def test_store_export_and_queries(sample_graph, tmp_path, samples, monkeypatch):
    G, stats = sample_graph
    store = GraphStore(tmp_path / "atlas.db")
    store.replace("code", G, stats)
    other = nx.DiGraph()
    other.add_node("SRS-0004", kind="requirement", name="SRS-0004", text="The TMF shall delete tracks.")
    store.replace("doors", other)
    loaded = store.load()
    assert set(loaded.nodes) == set(G.nodes) and set(loaded.edges) == set(G.edges)
    assert loaded.nodes["SRS-0004"]["text"] == "The TMF shall delete tracks."      # origins are merged
    assert store.builds()["code"]["files"] == 5
    store.delete("doors")
    assert "text" not in store.load().nodes["SRS-0004"]

    paths = export_graph(G, tmp_path / "out" / "code", ["graphml", "json"], stats)
    assert nx.read_graphml(paths[0]).nodes["tracks.update"]["requirements"] == "SRS-0002;SRS-0003"
    assert len(json.loads(paths[1].read_text())["edges"]) == G.number_of_edges()

    monkeypatch.chdir(samples.parent.parent)              # file paths are relative to the run directory
    g = KnowledgeGraph.open(tmp_path / "atlas.db")
    assert g.read_source("Delete_Track")["source"].splitlines()[0].endswith("procedure Delete_Track (I : Integer) is")
    assert g.read_source("track")["source"].startswith("type Track is record")
    assert [i["id"] for i in g.requirement("srs-0004")["traced_by"]["items"]] == \
        ["test_tracks.test_tracks", "tracks.delete_track"]
    assert g.call_chain("test_tracks", "log")["path"] is None             # a unit is not in the call graph
    assert g.call_chain("test_tracks.test_tracks", "log")["path"] == \
        ["test_tracks.test_tracks", "tracks.delete_track", "logger.log"]
    assert g.callees("update") == {"calls": ["logger.log"], "converts": ["tracks.hour_range"],
                                   "uses": ["tracks.trk_db"]}
    assert [i["id"] for i in g.untraced()["items"]] == ["logger.next_id"]
    assert g.find_symbol("delete")["items"][0]["id"] == "tracks.delete_track"
    assert g.search_docs("radar track")["items"][0]["id"] == "tracks.track"
    assert g.node("nothing")["error"] and g.unit_info("logger")["withs"] == ["ada.text_io"]


LEGACY = Path(__file__).resolve().parents[1] / "legacy"


@pytest.mark.skipif(not (LEGACY / "ada_graph.py").exists(), reason="legacy/ has been removed")
def test_same_graph_as_ada_graph(sample_graph, samples):
    """With the identifiers projected to simple names, the graph of the samples is the one
    of legacy/ada_graph.construir_grafo (to be deleted together with legacy/). The one
    difference: there, the test procedure Test_Tracks overwrote the node of its own unit."""
    sys.path.insert(0, str(LEGACY))
    try:
        import ada_graph
    finally:
        sys.path.remove(str(LEGACY))
    old, _ = ada_graph.construir_grafo(samples / "ada", requirement_pattern({}))
    new, _ = sample_graph
    simple = lambda n: new.nodes[n]["name"] if new.nodes[n].get("unit") else n
    assert {(simple(n), d["kind"]) for n, d in new.nodes(data=True)} - {("test_tracks", "unit")} == \
        {(n, d["kind"]) for n, d in old.nodes(data=True)}
    assert {(simple(u), simple(v), d["kind"]) for u, v, d in new.edges(data=True)} == \
        {(u, v, d["kind"]) for u, v, d in old.edges(data=True)}
    for n, d in new.nodes(data=True):
        if d["kind"] == "subprogram":
            o = old.nodes[simple(n)]
            assert (d["doc"], ";".join(d["requirements"]), d["start_line"], d["end_line"]) == \
                (o["doc"], o["requirements"], o["start_line"], o["end_line"]), n
