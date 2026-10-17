import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_ada")
nx = pytest.importorskip("networkx")

from atlas.code import AdaAnalyzer, requirement_pattern  # noqa: E402
from atlas.graph import GraphStore, GraphTools, KnowledgeGraph, build_code_graph, export_graph  # noqa: E402

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


def test_overloads_tasks_and_operators_in_the_graph(tmp_path):
    """The probe file of test_code: two Run of one package are one node with both bodies,
    the task and the operator are subprograms with their calls and requirements."""
    from test_code import PROBE
    (tmp_path / "probe.adb").write_bytes(PROBE)
    (tmp_path / "probe.ads").write_text("package Probe is\n   -- Runs.\n   -- Req: SRS-0016\n   procedure Run;\n"
                                        "   procedure Reset;\nend Probe;\n")
    G, stats = build_code_graph(_units(sorted(tmp_path.glob("*.ad?"))))
    run = G.nodes["probe.run"]
    assert (run["start_line"], run["end_line"], run["bodies"]) == (30, 41, [[30, 37], [38, 41]])
    assert run["requirements"] == ["SRS-0014", "SRS-0015", "SRS-0016"] and run["doc"] == "Runs.\nReq: SRS-0016"
    assert {v for _, v in G.out_edges("probe.run")} >= {"log", "put_line", "probe.reset", "SRS-0015", "SRS-0014"}
    assert ("probe.main_loop", "probe.run") in _edges(G, "calls") and "probe.t" not in G and "t" not in G
    assert (G.nodes["probe.main_loop"]["subprogram_kind"], G.nodes['probe."+"']["subprogram_kind"]) == \
        ("task", "operator")
    assert ("probe.main_loop", "SRS-0011") in _edges(G, "implements")
    assert ('probe."+"', "SRS-0013") in _edges(G, "implements") and "bodies" not in G.nodes["probe.seize"]
    assert stats["subprogram_kinds"] == {"entry": 1, "operator": 1, "procedure": 3, "protected": 1, "task": 1}
    assert stats["overloaded"] == 1 and stats["subprograms"] == 7
    g = KnowledgeGraph(G, root=tmp_path)
    source = g.read_source("probe.run")
    assert source["bodies"] == [{"file": str(tmp_path / "probe.adb"), "start": 30, "end": 37},
                                {"file": str(tmp_path / "probe.adb"), "start": 38, "end": 41}]
    assert source["source"].count("procedure Run") == 2 and "\n      ...\n" in source["source"]
    assert "Main_Loop" not in source["source"]
    assert g.summary("probe.main_loop")["subprogram_kind"] == "task" and "subprogram_kind" not in g.summary("probe.run")
    assert [i["id"] for i in g.impact("probe.run")["items"]] == ["probe.main_loop"]
    from dataclasses import asdict
    from atlas.index import code_records                      # the index has both bodies too
    record = next(r for r in code_records("probe", [asdict(u) for u in _units([tmp_path / "probe.adb"])],
                                          {str(tmp_path / "probe.adb"): PROBE.decode()}) if r["ref"] == "probe.run")
    assert "probe.adb:30-37, 38-41" in record["text"] and record["text"].count("procedure Run") == 2


USES_AND_ALIASES = {
    "store.ads": "package Store is\n   procedure Put (X : Integer);\n   procedure Write (X : Integer) renames Put;\n"
                 "end Store;\n",
    "store.adb": "package body Store is\n   procedure Put (X : Integer) is\n   begin\n      null;\n   end Put;\n"
                 "end Store;\n",
    "other.adb": "package body Other is\n   procedure Put (X : Integer) is\n   begin\n      null;\n   end Put;\n"
                 "end Other;\n",
    "stacks.adb": "package body Stacks is\n   procedure Push (X : Integer) is\n   begin\n      null;\n"
                  "   end Push;\nend Stacks;\n",
    "client.adb": """with Store; with Other; with Stacks; with Ada.Text_IO;
use Store;
package body Client is
   procedure Log (S : String) renames Ada.Text_IO.Put_Line;
   package Int_Stack is new Stacks (Integer);
   package TIO renames Ada.Text_IO;
   procedure Show (X : Integer) renames Other.Put;
   procedure Run is
   begin
      Put (1);
      Log ("x");
      Int_Stack.Push (2);
      Show (3);
      Store.Write (4);
      TIO.New_Line;
   end Run;
end Client;
""",
}


def test_use_clauses_and_aliases_in_the_resolution(tmp_path):
    for name, text in USES_AND_ALIASES.items():
        (tmp_path / name).write_text(text)
    G, stats = build_code_graph(_units(sorted(tmp_path.glob("*.ad?"))))
    calls = {v for u, v in _edges(G, "calls") if u == "client.run"}
    assert calls == {"store.put", "ada.text_io.put_line", "stacks.push", "other.put", "ada.text_io.new_line"}
    assert (stats["ambiguous_calls"], stats["calls_through_uses"], stats["calls_through_aliases"]) == (0, 1, 5)
    assert dict(stats["top_unresolved"]) == {"ada.text_io.new_line": 1, "ada.text_io.put_line": 1}
    assert "log" not in G and "int_stack.push" not in G and "tio.new_line" not in G

    # without the `use` clause, Put is in two units that Client imports: both
    (tmp_path / "client.adb").write_text(USES_AND_ALIASES["client.adb"].replace("use Store;\n", ""))
    G, stats = build_code_graph(_units(sorted(tmp_path.glob("*.ad?"))))
    assert {("client.run", "store.put"), ("client.run", "other.put")} <= _edges(G, "calls")
    assert (stats["ambiguous_calls"], stats["calls_through_uses"]) == (1, 0)

    # a name of the unit itself wins over a subprogram of another unit with the same name
    (tmp_path / "speeds.adb").write_text("package body Speeds is\n   subtype Knots is Integer range 0 .. 900;\n"
                                         "   procedure Set (I : Integer) is\n      K : Knots;\n   begin\n"
                                         "      K := Knots (I);\n   end Set;\nend Speeds;\n")
    (tmp_path / "units.adb").write_text("package body Units is\n   procedure Knots (X : Integer) is\n   begin\n"
                                        "      null;\n   end Knots;\nend Units;\n")
    G, _ = build_code_graph(_units(sorted(tmp_path.glob("*.ad?"))))
    assert ("speeds.set", "speeds.knots") in _edges(G, "converts") and ("speeds.set", "units.knots") not in G.edges


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
    assert [i["id"] for i in g.trace("srs-0004")["implemented_by"]["items"]] == \
        ["test_tracks.test_tracks", "tracks.delete_track"]                # without test_patterns, no tests
    assert g.call_chain("test_tracks", "log")["path"] is None             # a unit is not in the call graph
    assert g.call_chain("test_tracks.test_tracks", "log")["path"] == \
        ["test_tracks.test_tracks", "tracks.delete_track", "logger.log"]
    assert g.callees("update") == {"calls": ["logger.log"], "converts": ["tracks.hour_range"],
                                   "uses": ["tracks.trk_db"]}
    assert [i["id"] for i in g.untraced()["items"]] == ["logger.next_id"]
    assert g.find_symbol("delete")["items"][0]["id"] == "tracks.delete_track"
    assert g.search_docs("radar track")["items"][0]["id"] == "tracks.track"
    assert g.node("nothing")["error"] and g.unit_info("logger")["withs"] == ["ada.text_io"]



def test_graph_tools_follow_the_builds(sample_graph, tmp_path):
    from atlas import plugins
    from atlas.graph.tools import TOOL_NAMES
    G, stats = sample_graph
    tools = GraphTools(tmp_path / "atlas.db")                 # a long-lived process: atlas serve
    assert tools.callers("logger.log")["error"].startswith("there is no graph yet")
    store = GraphStore(tmp_path / "atlas.db")                 # atlas run, on another connection
    store.replace("code", G, stats)
    assert [i["id"] for i in tools.callers("logger.log")["items"]] == ["tracks.delete_track", "tracks.update"]
    assert tools.graph() is tools.graph()                     # read once per build
    G.add_edge("logger.next_id", "logger.log", kind="calls")
    store.replace("code", G, stats)                           # same statistics: the build time tells them apart
    assert [i["id"] for i in tools.callers("logger.log")["items"]] == ["logger.next_id", "tracks.delete_track",
                                                                       "tracks.update"]
    assert tools.trace("srs-0004")["id"] == "SRS-0004"
    assert tools.search_code_docs("radar track")["items"][0]["id"] == "tracks.track"
    store.delete("code")
    assert "error" in tools.get_node("tracks.update")
    plugins.load_plugins()
    assert set(TOOL_NAMES) <= set(plugins.available("tool")) and "read_file" not in TOOL_NAMES


LEGACY = Path(__file__).resolve().parents[1] / "legacy"


@pytest.mark.skipif(not (LEGACY / "ada_graph.py").exists(), reason="legacy/ has been removed")
def test_same_graph_as_ada_graph(sample_graph, samples):
    """With the identifiers projected to simple names, the graph of the samples is the one
    of legacy/ada_graph.construir_grafo (to be deleted together with legacy/). The one
    differences: there, the test procedure Test_Tracks overwrote the node of its own unit, and
    an element cited a requirement with a "traces" edge (here implements, or verifies in a test)."""
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
    kind = lambda k: "traces" if k in ("implements", "verifies") else k
    assert {(simple(u), simple(v), kind(d["kind"])) for u, v, d in new.edges(data=True)} == \
        {(u, v, d["kind"]) for u, v, d in old.edges(data=True)}
    for n, d in new.nodes(data=True):
        if d["kind"] == "subprogram":
            o = old.nodes[simple(n)]
            assert (d["doc"], ";".join(d["requirements"]), d["start_line"], d["end_line"]) == \
                (o["doc"], o["requirements"], o["start_line"], o["end_line"]), n


# --- the unified graph: code, DOORS and documents ----------------------------------------

DOORS = [   # rows of Store.requirements(): two modules, a heading and a hierarchy
    {"id": "SSS-0900", "module": "SSS", "text": "Capabilities", "parent": None,
     "attributes": {"kind": "heading", "heading": "Capabilities", "outline": "1"}},
    {"id": "SSS-0005", "module": "SSS", "text": "The system shall delete lost tracks.", "parent": "SSS-0900",
     "attributes": {"kind": "requirement", "heading": "", "outline": "1.1", "status": "Approved"}},
    {"id": "SSS-0006", "module": "SSS", "text": "The system shall record events.", "parent": "SSS-0900",
     "attributes": {"kind": "requirement", "outline": "1.2"}},
    {"id": "SRS-0001", "module": "SRS", "text": "A track has an id.", "parent": None, "attributes": {"outline": "0"}},
    {"id": "SRS-0002", "module": "SRS", "text": "The TMF shall update a track within 50 ms.", "parent": None,
     "attributes": {"outline": "1"}},
    {"id": "SRS-0003", "module": "SRS", "text": "Updates keep the hour.", "parent": "SRS-0002",
     "attributes": {"outline": "1.1"}},
    {"id": "SRS-0004", "module": "SRS", "text": "The TMF shall delete a track within 320 ms.", "parent": None,
     "attributes": {"outline": "2", "Verification": "Test"}},
]
LINKS = [
    {"source": "SRS-0004", "target": "SSS-0005", "type": "satisfies"},
    {"source": "SRS-0002", "target": "SSS-0006", "type": "satisfies"},
    {"source": "SRS-0004", "target": "SSS-0099", "type": "satisfies"},        # a module that was not read
    {"source": "SRS-0003", "target": "SRS-0002", "type": "references"},       # inside the module: no derivation
    {"source": "SRS-0004", "target": "SSS-0005", "type": "references"},       # the same pair again
]
SPEC = {"title": "Spec", "sections": [
    {"id": "001", "title": "1 Tracks", "path": ["1 Tracks"], "file": "sections/001-1-tracks.md", "page_start": 1,
     "page_end": 1, "body": "Req: SRS-0004 The TMF shall delete a track within 300 ms. Tracks.Delete_Track does "
                            "it; Update and Hour_Range too.",
     "analysis": {"requirements": [{"id": "srs-0004", "text": "The TMF shall delete a track within 300 ms."},
                                   {"id": "", "text": "no ID"}, {"id": "not an id", "text": "x"}]}},
    {"id": "002", "title": "2 Logs", "path": ["2 Logs"], "file": "sections/002-2-logs.md",
     "body": "Req: SRS-0103 Logs are kept 30 days. See Logger.Log.",
     "analysis": {"requirements": [{"id": "SRS-0103", "text": "Logs are kept 30 days."}]}},
]}


@pytest.fixture
def unified(samples, tmp_path):
    """The three origins in one atlas.db, as `atlas run` leaves them, and the graph read back."""
    from atlas.graph import build_docs_graph, build_doors_graph, code_names
    units = _units(sorted((samples / "ada").rglob("*.ad?")))
    store = GraphStore(tmp_path / "atlas.db")
    code = build_code_graph(units, test_patterns=["**/tests/**"])
    doors = build_doors_graph(DOORS, LINKS)
    docs = build_docs_graph([{"name": "spec.pdf", "path": "docs/spec.pdf", "source": "normas",
                              "out_dir": str(tmp_path / "spec_pdf"), "structure": SPEC}], code_names(units))
    for origin, (G, stats) in (("code", code), ("doors", doors), ("docs", docs)):
        store.replace(origin, G, stats)
    return store, KnowledgeGraph.open(tmp_path / "atlas.db"), {"code": code[1], "doors": doors[1], "docs": docs[1]}


def test_tests_and_requirement_ids_of_the_code(tmp_path):
    from atlas.doors import normalize_id
    from atlas.graph.build import is_test_file
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "p.adb").write_text("package body P is\n   -- Req: srs-0009\n   procedure Run is\n"
                                            "   begin\n      null;\n   end Run;\nend P;\n")
    (tmp_path / "tests" / "t.adb").write_text("with P;\nprocedure T is\n   -- Req: SRS-0009\nbegin\n"
                                              "   P.Run;\nend T;\n")
    files = sorted(tmp_path.rglob("*.adb"))
    G, stats = build_code_graph(_units(files), test_patterns=["**/tests/**"])
    assert G.nodes["t.t"]["kind"] == "test" and G.nodes["p.run"]["kind"] == "subprogram"
    assert _edges(G, "implements") == {("p.run", "SRS-0009")} and _edges(G, "verifies") == {("t.t", "SRS-0009")}
    assert (stats["subprograms"], stats["tests"], stats["test_files"]) == (1, 1, 1)
    G, _ = build_code_graph(_units(files), normalize=lambda r: normalize_id(r, "none"))     # id_normalize = "none"
    assert {"srs-0009", "SRS-0009"} <= set(G) and G.nodes["t.t"]["kind"] == "subprogram"
    assert is_test_file("tests/t.adb", ["**/tests/**"]) and not is_test_file("src/p.adb", ["**/tests/**"])


def test_the_three_origins_in_one_graph(unified, tmp_path):
    from atlas.graph import build_doors_graph, document_names
    store, g, stats = unified
    G = g.G
    assert stats["doors"].pop("id_shapes") == [{"prefix": "SRS", "separator": "-", "digits": 4, "count": 4,
                                                "example": "SRS-0001"},
                                               {"prefix": "SSS", "separator": "-", "digits": 4, "count": 2,
                                                "example": "SSS-0005"}]          # the heading SSS-0900 is not one
    assert stats["doors"] == {"nodes": 10, "edges": 11, "modules": 2, "requirements": 6, "headings": 1,
                              "information": 0, "linked_outside": 1, "duplicate_links": 1,
                              "edges_by_kind": {"contains": 7, "references": 1, "satisfies": 3}}
    assert (stats["docs"]["sections"], stats["docs"]["requirements"], stats["docs"]["mentions"]) == (2, 2, 3)
    assert G.nodes["SSS-0900"]["kind"] == "heading" and ("SSS-0900", "SSS-0005") in _edges(G, "contains")
    assert ("module:SRS", "SRS-0004") in _edges(G, "contains") and G.nodes["SRS-0004"]["outline"] == "2"
    assert "heading" not in G.nodes["SSS-0005"] and G.nodes["SSS-0005"]["attributes"] == {"status": "Approved"}
    assert _edges(G, "mentions") == {("doc:spec.pdf#001", "tracks.delete_track"),
                                     ("doc:spec.pdf#001", "tracks.hour_range"), ("doc:spec.pdf#002", "logger.log")}
    assert G.nodes["doc:spec.pdf#001"]["requirements"] == [{"id": "SRS-0004",
                                                           "text": "The TMF shall delete a track within 300 ms."}]
    assert G.nodes["doc:spec.pdf#001"]["file"] == str(tmp_path / "spec_pdf" / "sections" / "001-1-tracks.md")
    assert document_names([{"unit": "a", "path": "x/s.pdf"}, {"unit": "b", "path": "y/s.pdf"},
                           {"unit": "c", "path": "y/t.md"}]) == {"a": "x/s.pdf", "b": "y/s.pdf", "c": "t.md"}

    # the kind of DOORS prevails; what no loaded origin gives a kind to is dropped
    heading = nx.DiGraph()
    heading.add_node("SRS-0002", kind="heading", name="SRS-0002", module="SRS")
    store.replace("zdoors", heading)
    assert store.load().nodes["SRS-0002"]["kind"] == "heading"
    store.delete("zdoors")
    store.delete("code")
    assert "tracks.delete_track" not in store.load() and not _edges(store.load(), "mentions")
    assert build_doors_graph([], [])[1]["nodes"] == 0


def test_trace_and_trace_code(unified):
    _, g, _ = unified
    t = g.trace("srs-0004")
    assert (t["in_doors"], t["module"], t["parent"], t["attributes"]) == (True, "SRS", None, {"Verification": "Test"})
    assert t["links_out"] == [{"id": "SSS-0005", "type": "satisfies"}, {"id": "SSS-0099", "type": "satisfies"}]
    assert t["documents"] == [{"id": "doc:spec.pdf#001", "document": "spec.pdf", "section": "001", "title": "1 Tracks",
                               "text": "The TMF shall delete a track within 300 ms.",
                               "values_differ": {"doors": ["320"], "document": ["300"]}}]
    assert [i["id"] for i in t["implemented_by"]["items"]] == ["tracks.delete_track"]
    assert t["verified_by"] == [{"id": "test_tracks.test_tracks", "how": "tag"}]
    assert g.trace("SRS-0003")["parent"] == "SRS-0002" and g.trace("SRS-0002")["children"] == ["SRS-0003"]
    assert g.trace("SSS-0005")["links_in"] == [{"id": "SRS-0004", "type": "satisfies"}]
    t = g.trace("SRS-0103")                                       # in the code and a document, not in DOORS
    assert t["in_doors"] is False and "values_differ" not in t["documents"][0]
    assert t["verified_by"] == [{"id": "test_tracks.test_tracks", "how": "calls"}]   # test -> delete_track -> log
    assert g.trace("SRS-004")["similar"][0] == "SRS-0004"

    c = g.trace_code("Delete_Track")
    assert [r["id"] for r in c["requirements"]] == ["SRS-0004"] and c["tests"] == ["test_tracks.test_tracks"]
    assert c["callees"]["calls"] == ["logger.log"] and c["documents"][0]["via"] == "mentions"
    c = g.trace_code("test_tracks.test_tracks")
    assert c["covers"] == ["SRS-0004", "SRS-0103"] and "logger.log" in c["exercises"]
    assert [r["id"] for r in g.trace_code("tracks")["requirements"]] == ["SRS-0001", "SRS-0002", "SRS-0003",
                                                                         "SRS-0004"]
    assert "use trace" in g.trace_code("SRS-0004")["error"]


def test_impact_of_code_and_of_requirements(unified):
    _, g, _ = unified
    i = g.impact("Logger.Log")
    assert [(x["id"], x["distance"]) for x in i["items"]] == [("tracks.delete_track", 1), ("tracks.update", 1),
                                                             ("test_tracks.test_tracks", 2)]
    assert [r["id"] for r in i["requirements"]] == ["SRS-0103", "SRS-0004", "SRS-0002", "SRS-0003", "SSS-0005",
                                                    "SSS-0099", "SSS-0006"]
    assert i["requirements"][4]["via"] == ["SRS-0004"] and i["tests"] == ["test_tracks.test_tracks"]
    assert [d["id"] for d in i["documents"]] == ["doc:spec.pdf#002", "doc:spec.pdf#001"]
    assert [x["id"] for x in g.impact("trk_db")["items"]] == ["tracks.delete_track", "tracks.update",
                                                              "test_tracks.test_tracks"]    # who uses a global
    r = g.impact("SSS-0006")
    assert [x["id"] for x in r["requirements"]] == ["SRS-0002", "SRS-0003"]       # satisfied by, then its child
    assert [x["id"] for x in r["items"]] == ["tracks.update"] and r["tests"] == []
    assert "impact takes" in g.impact("doc:spec.pdf")["error"]


def test_matrix_and_gaps(unified):
    _, g, _ = unified
    m = g.matrix("SRS")
    assert [r["id"] for r in m["rows"]] == ["SRS-0001", "SRS-0002", "SRS-0003", "SRS-0004"]     # DOORS order
    assert m["rows"][3] == {"id": "SRS-0004", "text": "The TMF shall delete a track within 320 ms.",
                            "verification": "Test", "derived": [], "code": ["tracks.delete_track"],
                            "tests": ["test_tracks.test_tracks"], "covering_tests": [],
                            "documents": ["spec.pdf §1 Tracks"]}
    assert m["summary"] == {"requirements": 4, "with_code": 4, "with_tests": 1, "with_documents": 1}
    assert m["gaps"]["without_tests"]["items"] == ["SRS-0001", "SRS-0002", "SRS-0003"]
    m = g.matrix("SSS")                                           # implemented through the SRS that satisfy it
    assert [(r["id"], r["derived"], r["code"]) for r in m["rows"]] == [("SSS-0005", ["SRS-0004"], []),
                                                                      ("SSS-0006", ["SRS-0002"], [])]
    assert m["summary"]["with_code"] == 2 and m["gaps"]["without_tests"]["items"] == ["SSS-0006"]
    assert g.matrix("NOPE")["modules"] == ["SRS", "SSS"]
    page = g.matrix("SRS", limit=2, offset=2)
    assert [r["id"] for r in page["rows"]] == ["SRS-0003", "SRS-0004"] and not page["truncated"]

    gaps = g.gaps()
    assert gaps["requirements_without_code"]["count"] == 0
    assert gaps["requirements_without_tests"]["items"] == ["SRS-0001", "SRS-0002", "SRS-0003", "SSS-0006"]
    assert gaps["code_without_requirement"]["items"] == ["logger.next_id"]
    assert gaps["cited_in_code_not_in_doors"]["items"] == [{"id": "SRS-0103", "cited_by": ["logger.log"]}]
    assert gaps["stated_in_documents_not_in_doors"]["items"] == [{"id": "SRS-0103", "documents": ["spec.pdf §2 Logs"]}]
    assert gaps["values_differ"]["items"] == [{"id": "SRS-0004", "section": "spec.pdf §1 Tracks", "doors": ["320"],
                                               "document": ["300"]}]
    assert g.gaps("SSS")["requirements_without_tests"]["items"] == ["SSS-0006"]
    assert [r["id"] for r in g.list_requirements(module="SSS")["items"]] == ["SSS-0005", "SSS-0006"]
    assert g.list_requirements(prefix="srs-01")["items"] == [{"id": "SRS-0103", "code": 1, "tests": 1, "documents": 1}]


def test_neighbors_and_communities(unified, tmp_path):
    from atlas.graph import find_communities
    _, g, _ = unified
    n = g.neighbors("SRS-0004")
    assert {x["id"] for x in n["nodes"]} == {"SRS-0004", "SSS-0005", "SSS-0099", "module:SRS", "doc:spec.pdf#001",
                                             "tracks.delete_track", "test_tracks.test_tracks"}
    assert {"source": "tracks.delete_track", "target": "SRS-0004", "kind": "implements"} in n["edges"]
    n = g.neighbors("SRS-0004", depth=2, kinds="implements,verifies")
    assert {x["id"] for x in n["nodes"]} == {"SRS-0004", "tracks.delete_track", "test_tracks.test_tracks"}
    assert g.neighbors("tracks", depth=2, limit=3)["truncated"]
    mermaid = g.neighbors("SRS-0004", format="mermaid")["mermaid"]
    assert mermaid.startswith("flowchart LR") and '(["SRS-0004' in mermaid and "-->|implements|" in mermaid
    assert "error" in g.neighbors("SRS-0004", format="svg")
    paths = export_graph(g.G, tmp_path / "out" / "atlas", ["graphml", "mermaid"])
    assert nx.read_graphml(paths[0]).nodes["SSS-0005"]["attributes"] == '{"status": "Approved"}'
    assert paths[1].read_text().startswith("flowchart LR")

    groups = find_communities(g.G)
    assert groups == find_communities(g.G) and "logger.log" in groups and "ada.text_io.put_line" not in groups
    c = g.communities()
    assert c["count"] == len(set(groups.values())) and sum(x["size"] for x in c["items"]) == len(groups)
    assert c["items"][0]["size"] >= c["items"][-1]["size"]
