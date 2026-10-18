"""The code graph, built from the CodeUnits of every file.

Nodes (`kind`): unit (package / compilation unit), subprogram, test (a subprogram of a file
that matches [sources.code] test_patterns), type, variable, constant, requirement, external
(what is called or imported but not in the project).
Edges (`kind`): defines (unit -> what it declares), calls, converts (type conversion),
uses (access to a package-level variable), withs (unit -> imported unit), implements
(element -> requirement ID found in its comments) and verifies (test -> requirement ID
found in its comments).

Identifiers: units by their name ("tracks", "tracks.db"); what a unit declares by
"unit.name" ("tracks.update"), so that two subprograms with the same name in two packages
are two nodes, and two with the same name in one package (overloads) are one: with the
union of their requirements and calls, the first line of the first, the last line of the
last and `bodies`, the [start, end] of each one (and its file, if it is not the file of the
node). A subprogram node has its `subprogram_kind` (procedure, function, operator, task,
entry, protected); externals by their text, in lower case like everything of the code;
requirements by their ID normalised like those of DOORS (upper case by default), so that
the code, DOORS and the documents meet in the same node.

Every `Name (...)` of a body is classified by what Name is: subprogram -> calls, type ->
converts, variable -> uses, a name of the language (the `predefined` of the analyzer of the
caller's language: Integer, Float...) -> ignored, unknown -> external call. A name is
looked for first as written (with its package) and in the caller's own unit; then, if it is
an alias of the unit (a renaming or a generic instance: `Log` renames Ada.Text_IO.Put_Line,
`Int_IO.Put` with Int_IO an instance of Ada.Text_IO.Integer_IO), as what it stands for; a
name without a package, then in the units of the caller's `use` clauses; and last in every
unit. If several units have it, the ones the caller imports with `with` are preferred, and
if it is still ambiguous the edge goes to all of them (an impact analysis must not miss
any). An alias of something outside the project is an external call by its full name. A call
found only in units the caller does not import is counted apart (`unimported_calls`): it is the
one guess of the resolution that `ambiguous_calls` does not show. The analyzers are those of
the plugin registry, so `atlas.code` (or the plugins) must be loaded first.
"""
from __future__ import annotations

import fnmatch
from collections import Counter, defaultdict
from typing import Callable, Iterable

import networkx as nx

from atlas import plugins
from atlas.doors import id_shapes, normalize_id
from atlas.plugins import CodeUnit, call_name

__all__ = ["CODE_KINDS", "build_code_graph", "find_communities", "is_test_file"]

CODE_KINDS = ("unit", "subprogram", "test", "type", "variable", "constant")
DECLARED = ("subprogram", "type", "variable")      # what a unit declares, by resolution order
EDGE_FOR = {"subprogram": "calls", "type": "converts", "variable": "uses"}


def is_test_file(file: str, patterns: Iterable[str]) -> bool:
    """Whether a file matches one of the test_patterns ("**/tests/**")."""
    f = file.replace("\\", "/")
    return any(fnmatch.fnmatch(f, p) or fnmatch.fnmatch("/" + f, p) for p in patterns)


def _predefined(language: str) -> frozenset[str]:
    """The names of a language that are never calls, as its analyzer says."""
    try:
        return frozenset(plugins.get("code_analyzer", language).predefined)
    except LookupError:                    # an analyzer that is not loaded: nothing is predefined
        return frozenset()


def build_code_graph(units: Iterable[CodeUnit | dict], test_patterns: Iterable[str] = (),
                     normalize: Callable[[str], str] = normalize_id) -> tuple[nx.DiGraph, dict]:
    """(graph, statistics) from the analysis of every file. The subprograms of the files
    that match `test_patterns` are tests; `normalize` gives the requirement IDs the form
    of DOORS ([sources.doors.convention] id_normalize)."""
    units = sorted((u if isinstance(u, CodeUnit) else CodeUnit.from_dict(u) for u in units), key=lambda u: u.file)
    test_patterns = list(test_patterns)
    tests = {u.file for u in units if is_test_file(u.file, test_patterns)}
    G = nx.DiGraph()

    # Documentation and requirements declared in the specs, by (unit, name)
    spec: dict[tuple[str, str], dict] = {}
    for u in units:
        for d in u.declarations:
            e = spec.setdefault((u.unit, d["name"]), {"doc": "", "reqs": set(), "spec_file": u.file,
                                                     "kind": d.get("kind", "")})
            if d["doc"] and not e["doc"]:
                e["doc"] = d["doc"]
            e["reqs"].update(normalize(r) for r in d["reqs"])

    def cites(node: str, reqs: Iterable[str]) -> None:
        kind = "verifies" if G.nodes[node]["kind"] == "test" else "implements"
        for r in reqs:
            if r not in G:
                G.add_node(r, kind="requirement", name=r)
            G.add_edge(node, r, kind=kind)

    # The bodies of each subprogram: two with the same unit and name (overloads) are one node
    bodies: dict[str, list[tuple[CodeUnit, dict]]] = defaultdict(list)
    for u in units:
        for s in u.subprograms:
            bodies[f"{u.unit}.{s['name']}"].append((u, s))

    # Pass 1: what is defined (units, types, variables, subprograms)
    index: dict[str, dict[str, list[str]]] = {k: defaultdict(list) for k in DECLARED}   # kind -> name -> ids
    imports: dict[str, set[str]] = defaultdict(set)
    uses: dict[str, list[str]] = defaultdict(list)          # the spec's clauses count in the body too
    aliases: dict[str, dict[str, str]] = defaultdict(dict)
    for u in units:
        if u.unit not in G:
            G.add_node(u.unit, kind="unit", name=u.unit, files=[])
        G.nodes[u.unit]["files"].append(u.file)
        imports[u.unit].update(u.imports)
        uses[u.unit] += [x for x in u.uses if x not in uses[u.unit]]
        for name, target in u.aliases.items():
            aliases[u.unit].setdefault(name, target)
        for t in u.types:
            tid = f"{u.unit}.{t['name']}"
            if tid in G:
                continue
            reqs = sorted({normalize(r) for r in t.get("reqs", [])})
            G.add_node(tid, kind="type", name=t["name"], unit=u.unit, file=u.file, line=t["line"],
                       record=t.get("record", False), doc=t.get("doc", ""), source=t.get("source", ""),
                       requirements=reqs)
            index["type"][t["name"]].append(tid)
            G.add_edge(u.unit, tid, kind="defines")
            cites(tid, reqs)
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
            group = bodies[sid]
            if group[0][1] is not s:                   # a later overload: already in the node of the first
                continue
            sp = spec.get((u.unit, s["name"]), {})
            reqs = sorted({normalize(r) for _, b in group for r in b["reqs"]} | sp.get("reqs", set()))
            if sid not in G:
                index["subprogram"][s["name"]].append(sid)
            here = [b for v, b in group if v.file == u.file]
            extra = {}
            if len(group) > 1:                         # [start, end], and the file if it is another one
                extra["bodies"] = [[b["start_line"], b["end_line"], *([v.file] if v.file != u.file else [])]
                                   for v, b in group]
            G.add_node(sid, kind="test" if u.file in tests else "subprogram", name=s["name"], unit=u.unit,
                       subprogram_kind=s.get("kind") or sp.get("kind", ""),
                       file=u.file, spec_file=sp.get("spec_file", ""), start_line=s["start_line"],
                       end_line=here[-1]["end_line"],    # the doc of the spec first
                       doc=sp.get("doc") or next((b["doc"] for _, b in group if b["doc"]), ""),
                       requirements=reqs, **({"signature": s["signature"]} if s.get("signature") else {}), **extra)
            G.add_edge(u.unit, sid, kind="defines")
            cites(sid, reqs)

    # Subprograms declared in a spec with no body found (e.g. imported or abstract)
    for (unit, name), sp in spec.items():
        sid = f"{unit}.{name}"
        if sid in G:
            continue
        reqs = sorted(sp["reqs"])
        G.add_node(sid, kind="test" if sp["spec_file"] in tests else "subprogram", name=name, unit=unit,
                   subprogram_kind=sp["kind"], file=sp["spec_file"], spec_file=sp["spec_file"], start_line=0,
                   end_line=0, doc=sp["doc"], requirements=reqs)
        index["subprogram"][name].append(sid)
        G.add_edge(unit, sid, kind="defines")
        cites(sid, reqs)

    def own(call: str, unit: str) -> tuple[list[str], str] | None:
        """The element a name is in its own scope: qualified, or of the unit itself."""
        prefix, _, simple = call.rpartition(".")
        for kind in DECLARED:
            ids = index[kind].get(simple, [])
            if prefix and f"{prefix}.{simple}" in ids:
                return [f"{prefix}.{simple}"], EDGE_FOR[kind]
            if f"{unit}.{simple}" in ids:
                return [f"{unit}.{simple}"], EDGE_FOR[kind]
        return None

    def unalias(call: str, unit: str) -> tuple[str, str] | None:
        """(what an alias stands for, the unit where it is read) or None: `Log` renaming
        Ada.Text_IO.Put_Line, `Int_IO.Put` with Int_IO an instance of Ada.Text_IO.Integer_IO,
        `Pkg.Log` with Log a renaming of Pkg."""
        prefix, _, simple = call.rpartition(".")
        if not prefix and simple in aliases[unit]:
            return aliases[unit][simple], unit
        head, _, rest = call.partition(".")
        if prefix and head in aliases[unit]:
            return f"{aliases[unit][head]}.{rest}", unit
        if prefix and simple in aliases.get(prefix, {}):
            return aliases[prefix][simple], prefix
        return None

    def resolve(call: str, unit: str, predefined: frozenset[str], depth: int = 0) -> tuple[list[str], str, str, bool]:
        """(targets, edge kind, how: "", "alias" or "use", whether it was found only in units
        the caller does not import)."""
        found = own(call, unit)
        if found:
            return (*found, "", False)
        alias = unalias(call, unit) if depth < 5 else None
        if alias:
            targets, kind, _, guessed = resolve(*alias, predefined, depth + 1)
            return targets, kind, "alias", guessed
        prefix, _, simple = call.rpartition(".")
        if not prefix:                          # the packages of its `use` clauses, then everyone
            for kind in DECLARED:
                ids = index[kind].get(simple, [])
                used = [f"{x}.{simple}" for x in uses[unit] if f"{x}.{simple}" in ids]
                if used:
                    return used, EDGE_FOR[kind], "use", False
        for kind in DECLARED:
            found = index[kind].get(simple, [])
            if found:
                visible = [i for i in found if G.nodes[i]["unit"] in imports[unit]]
                return (visible or found), EDGE_FOR[kind], "", not visible
        if simple in predefined:
            return [], "predefined", "", False
        return [], "external:" + call if depth else "external", "", False   # an alias of something outside: its name

    # Pass 2: edges
    counts = {"calls": 0, "converts": 0, "uses": 0, "predefined": 0}
    unresolved: dict[str, int] = defaultdict(int)
    ambiguous = unimported = 0
    through = {"alias": 0, "use": 0}            # calls resolved through an alias or a `use` clause
    predefined = {lang: _predefined(lang) for lang in {u.language for u in units}}
    for u in units:
        for w in u.imports:
            if w not in G:
                G.add_node(w, kind="external", name=w)
            G.add_edge(u.unit, w, kind="withs")
        for s in u.subprograms:
            sid = f"{u.unit}.{s['name']}"
            for call in dict.fromkeys(call_name(c) for c in s["calls"]):     # each name once, in order
                targets, kind, how, guessed = resolve(call, u.unit, predefined[u.language])
                unimported += guessed
                if how:
                    through[how] += 1
                if kind == "predefined":
                    counts["predefined"] += 1
                    continue
                if kind.startswith("external"):
                    name = kind.partition(":")[2] or call         # an alias: what it stands for
                    if name not in G:
                        G.add_node(name, kind="external", name=name)
                    unresolved[name] += 1
                    targets, kind = [name], "calls"
                elif len(targets) > 1:
                    ambiguous += 1
                for t in targets:
                    G.add_edge(sid, t, kind=kind)
                counts[kind] += 1

    raw_ids = [r for u in units for item in (*u.declarations, *u.types, *u.subprograms) for r in item.get("reqs", [])]
    nodes_of = lambda kind: [(n, d) for n, d in G.nodes(data=True) if d.get("kind") == kind]
    subs = nodes_of("subprogram")
    stats = {
        "files": len(units),
        "test_files": len(tests),
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "subprograms": len(subs),
        "subprogram_kinds": dict(sorted(Counter(d.get("subprogram_kind") or "?" for _, d in
                                                [*subs, *nodes_of("test")]).items())),
        "overloaded": sum(1 for group in bodies.values() if len(group) > 1),
        "tests": len(nodes_of("test")),
        "types": len(nodes_of("type")),
        "variables": len(nodes_of("variable")) + len(nodes_of("constant")),
        "externals": len(nodes_of("external")),
        "requirements": len(nodes_of("requirement")),
        "edges_by_kind": counts,
        "ambiguous_calls": ambiguous,
        "calls_through_aliases": through["alias"],
        "calls_through_uses": through["use"],
        "unimported_calls": unimported,
        "subprograms_without_doc": sum(1 for _, d in subs if not d.get("doc")),
        "subprograms_without_requirement": sum(1 for _, d in subs if not d.get("requirements")),
        "unresolved_calls": sum(unresolved.values()),
        "top_unresolved": sorted(unresolved.items(), key=lambda kv: (-kv[1], kv[0]))[:15],
        "top_called": sorted(((n, sum(1 for _, _, e in G.in_edges(n, data=True) if e["kind"] == "calls"))
                              for n, _ in subs), key=lambda kv: (-kv[1], kv[0]))[:15],
        "top_requirements": sorted(((n, G.in_degree(n)) for n, _ in nodes_of("requirement")),
                                   key=lambda kv: (-kv[1], kv[0]))[:15],
        "id_shapes": id_shapes(raw_ids),      # as written in the tags, before normalising them
    }
    return G, stats


def find_communities(G: nx.DiGraph) -> dict[str, int]:
    """Groups of code that work together: Louvain (with a fixed seed, so that the groups do
    not change from one build to the next) over the code graph without direction, with its
    calls, conversions, uses, withs and what each unit defines; externals are left out, or
    a library that everything calls would join it all. node -> group, 0 the largest."""
    code = nx.Graph()
    for u, v, e in G.edges(data=True):
        if e.get("kind") in ("calls", "converts", "uses", "withs", "defines") and \
                G.nodes[u].get("kind") in CODE_KINDS and G.nodes[v].get("kind") in CODE_KINDS:
            code.add_edge(u, v)
    groups = sorted(nx.community.louvain_communities(code, seed=0), key=lambda g: (-len(g), min(g))) if code else []
    return {n: i for i, group in enumerate(groups) for n in group}
