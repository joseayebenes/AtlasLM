"""The code graph, built from the CodeUnits of every file (the graph of legacy/ada_graph.py).

Nodes (`kind`): unit (package / compilation unit), subprogram, type, variable, constant,
requirement, external (what is called or imported but not in the project).
Edges (`kind`): defines (unit -> what it declares), calls, converts (type conversion),
uses (access to a package-level variable), withs (unit -> imported unit), traces
(element -> requirement ID found in its comments).

Identifiers: units by their name ("tracks", "tracks.db"); what a unit declares by
"unit.name" ("tracks.update"), so that two subprograms with the same name in two packages
are two nodes; requirements and externals by their text. Everything in lower case except
requirement IDs, which keep the case they were written with.

Every `Name (...)` of a body is classified by what Name is: subprogram -> calls, type ->
converts, variable -> uses, predefined type -> ignored, unknown -> external call. A name
without a package is looked for first in the caller's own unit, then in every unit; if
several units have it, the ones the caller imports with `with` are preferred, and if it is
still ambiguous the edge goes to all of them (an impact analysis must not miss any).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

import networkx as nx

from atlas.code.ada import PREDEFINED_TYPES
from atlas.plugins import CodeUnit

__all__ = ["build_code_graph"]

DECLARED = ("subprogram", "type", "variable")      # what a unit declares, by resolution order
EDGE_FOR = {"subprogram": "calls", "type": "converts", "variable": "uses"}


def build_code_graph(units: Iterable[CodeUnit | dict]) -> tuple[nx.DiGraph, dict]:
    """(graph, statistics) from the analysis of every file."""
    units = sorted((u if isinstance(u, CodeUnit) else CodeUnit(**u) for u in units), key=lambda u: u.file)
    G = nx.DiGraph()

    # Documentation and requirements declared in the specs, by (unit, name)
    spec: dict[tuple[str, str], dict] = {}
    for u in units:
        for d in u.declarations:
            e = spec.setdefault((u.unit, d["name"]), {"doc": "", "reqs": set(), "spec_file": u.file})
            if d["doc"] and not e["doc"]:
                e["doc"] = d["doc"]
            e["reqs"].update(d["reqs"])

    def traces(node: str, reqs: Iterable[str]) -> None:
        for r in reqs:
            if r not in G:
                G.add_node(r, kind="requirement", name=r)
            G.add_edge(node, r, kind="traces")

    # Pass 1: what is defined (units, types, variables, subprograms)
    index: dict[str, dict[str, list[str]]] = {k: defaultdict(list) for k in DECLARED}   # kind -> name -> ids
    imports: dict[str, set[str]] = defaultdict(set)
    for u in units:
        if u.unit not in G:
            G.add_node(u.unit, kind="unit", name=u.unit, files=[])
        G.nodes[u.unit]["files"].append(u.file)
        imports[u.unit].update(u.imports)
        for t in u.types:
            tid = f"{u.unit}.{t['name']}"
            if tid in G:
                continue
            G.add_node(tid, kind="type", name=t["name"], unit=u.unit, file=u.file, line=t["line"],
                       record=t.get("record", False), doc=t.get("doc", ""), source=t.get("source", ""),
                       requirements=t.get("reqs", []))
            index["type"][t["name"]].append(tid)
            G.add_edge(u.unit, tid, kind="defines")
            traces(tid, t.get("reqs", []))
        for v in u.variables:
            vid = f"{u.unit}.{v['name']}"
            if vid in G:
                continue
            G.add_node(vid, kind="constant" if v.get("constant") else "variable", name=v["name"], unit=u.unit,
                       file=u.file, line=v["line"], source=v.get("source", ""))
            index["variable"][v["name"]].append(vid)
            G.add_edge(u.unit, vid, kind="defines")
        for s in u.subprograms:
            sid = f"{u.unit}.{s['name']}"
            sp = spec.get((u.unit, s["name"]), {})
            reqs = sorted(set(s["reqs"]) | sp.get("reqs", set()))
            if sid not in G:
                index["subprogram"][s["name"]].append(sid)
            G.add_node(sid, kind="subprogram", name=s["name"], unit=u.unit, file=u.file,
                       spec_file=sp.get("spec_file", ""), start_line=s["start_line"], end_line=s["end_line"],
                       doc=sp.get("doc") or s["doc"], requirements=reqs)    # the doc of the spec first
            G.add_edge(u.unit, sid, kind="defines")
            traces(sid, reqs)

    # Subprograms declared in a spec with no body found (e.g. imported or abstract)
    for (unit, name), sp in spec.items():
        sid = f"{unit}.{name}"
        if sid in G:
            continue
        reqs = sorted(sp["reqs"])
        G.add_node(sid, kind="subprogram", name=name, unit=unit, file=sp["spec_file"], spec_file=sp["spec_file"],
                   start_line=0, end_line=0, doc=sp["doc"], requirements=reqs)
        index["subprogram"][name].append(sid)
        G.add_edge(unit, sid, kind="defines")
        traces(sid, reqs)

    def resolve(call: str, unit: str) -> tuple[list[str], str]:
        prefix, _, simple = call.rpartition(".")
        for kind in DECLARED:
            ids = index[kind]
            if prefix and f"{prefix}.{simple}" in ids.get(simple, []):
                return [f"{prefix}.{simple}"], EDGE_FOR[kind]
            if f"{unit}.{simple}" in ids.get(simple, []):
                return [f"{unit}.{simple}"], EDGE_FOR[kind]
            found = ids.get(simple, [])
            if found:
                visible = [i for i in found if G.nodes[i]["unit"] in imports[unit]]
                return (visible or found), EDGE_FOR[kind]
        return [], "predefined" if simple in PREDEFINED_TYPES else "external"

    # Pass 2: edges
    counts = {"calls": 0, "converts": 0, "uses": 0, "predefined": 0}
    unresolved: dict[str, int] = defaultdict(int)
    ambiguous = 0
    for u in units:
        for w in u.imports:
            if w not in G:
                G.add_node(w, kind="external", name=w)
            G.add_edge(u.unit, w, kind="withs")
        for s in u.subprograms:
            sid = f"{u.unit}.{s['name']}"
            for call in s["calls"]:
                targets, kind = resolve(call, u.unit)
                if kind == "predefined":
                    counts["predefined"] += 1
                    continue
                if kind == "external":
                    if call not in G:
                        G.add_node(call, kind="external", name=call)
                    unresolved[call] += 1
                    targets, kind = [call], "calls"
                elif len(targets) > 1:
                    ambiguous += 1
                for t in targets:
                    G.add_edge(sid, t, kind=kind)
                counts[kind] += 1

    nodes_of = lambda kind: [(n, d) for n, d in G.nodes(data=True) if d.get("kind") == kind]
    subs = nodes_of("subprogram")
    stats = {
        "files": len(units),
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "subprograms": len(subs),
        "types": len(nodes_of("type")),
        "variables": len(nodes_of("variable")) + len(nodes_of("constant")),
        "externals": len(nodes_of("external")),
        "requirements": len(nodes_of("requirement")),
        "edges_by_kind": counts,
        "ambiguous_calls": ambiguous,
        "subprograms_without_doc": sum(1 for _, d in subs if not d.get("doc")),
        "subprograms_without_requirement": sum(1 for _, d in subs if not d.get("requirements")),
        "unresolved_calls": sum(unresolved.values()),
        "top_unresolved": sorted(unresolved.items(), key=lambda kv: (-kv[1], kv[0]))[:15],
        "top_called": sorted(((n, sum(1 for _, _, e in G.in_edges(n, data=True) if e["kind"] == "calls"))
                              for n, _ in subs), key=lambda kv: (-kv[1], kv[0]))[:15],
        "top_requirements": sorted(((n, G.in_degree(n)) for n, _ in nodes_of("requirement")),
                                   key=lambda kv: (-kv[1], kv[0]))[:15],
    }
    return G, stats
