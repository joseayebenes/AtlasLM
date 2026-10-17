"""The requirements and documents parts of the graph, next to the code graph of build.py.

build_doors_graph (origin "doors"): the requirements of every module read by atlas.doors.
  Nodes: module ("module:SRS_TMS"), requirement (its ID; text, module, parent and the
  attributes), heading and information (the DOORS objects that are not requirements).
  Edges: contains (module -> its top-level objects, parent -> child) and one edge per link,
  source -> target, with the type of the link as kind (satisfies, refines, references...).
  If two links join the same pair, the first one read stays: the link of the source before
  the reference found in the text. What each type of link means for the queries
  ([sources.doors.convention] link_semantics: derives, verifies or ignore) goes in the
  statistics of the build, so that every reader of the graph takes the same convention.

build_docs_graph (origin "docs"): the documents processed by atlas.docs.
  Nodes: document ("doc:hmi_manual.md"), section ("doc:hmi_manual.md#003": title, path,
  pages, the file of its Markdown and the requirements it states, with their text) and the
  requirements they state.
  Edges: contains (document -> section), declares (section -> requirement) and mentions
  (section -> code element whose name it writes: "Tracks.Delete_Track", or a name with an
  underscore that only one element has, "Delete_Track"; single words such as "Update" are
  too common to tell).

A requirement is the same node in the three origins (code, docs, doors): GraphStore.load
merges them, and the kind of DOORS (heading, information) prevails.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable

import networkx as nx

from atlas.code.common import REQ_ID_SHAPE
from atlas.doors import normalize_id
from atlas.plugins import CodeUnit

__all__ = ["build_docs_graph", "build_doors_graph", "code_names", "document_names", "graph_convention"]

OBJECT_KINDS = ("heading", "information")     # the DOORS objects that are not requirements
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*")


def graph_convention(convention: dict | None = None) -> dict:
    """What the queries of the graph take from [sources.doors.convention]:
    link_semantics, the types of link that derive a requirement from another (satisfies,
    refines...; empty: every type that is neither in verifies nor in ignore), those of a
    test procedure that verifies a requirement, and those that are only references (by
    default "references" and the inline_reference_type). Idempotent: it takes its own
    result."""
    conv = convention or {}
    given = conv.get("link_semantics") or {}
    inline = conv.get("inline_reference_type") or "references"
    semantics = {"derives": list(given.get("derives") or []),
                 "verifies": list(given.get("verifies", ["verifies"])),
                 "ignore": list(given.get("ignore", sorted({"references", inline})))}
    return {"link_semantics": semantics}


def build_doors_graph(requirements: Iterable[dict], links: Iterable[dict],
                      convention: dict | None = None) -> tuple[nx.DiGraph, dict]:
    """(graph, statistics) from the rows of Store.requirements() and Store.links(); the
    statistics carry graph_convention(convention)."""
    G = nx.DiGraph()
    reqs = list(requirements)
    for r in reqs:
        attributes = dict(r.get("attributes") or {})
        kind = attributes.pop("kind", "")
        extra = {k: v for k in ("heading", "outline", "level") if (v := attributes.pop(k, None)) not in (None, "")}
        G.add_node(r["id"], kind=kind if kind in OBJECT_KINDS else "requirement", name=r["id"], module=r["module"],
                   text=r["text"] or "", parent=r.get("parent") or "", raw_id=r.get("raw_id") or "",
                   attributes=attributes, **extra)
    for module in sorted({r["module"] for r in reqs}):
        G.add_node(f"module:{module}", kind="module", name=module)
    for r in reqs:
        parent = r.get("parent")
        inside = parent and parent in G and G.nodes[parent].get("module") == r["module"]
        G.add_edge(parent if inside else f"module:{r['module']}", r["id"], kind="contains")

    duplicates = missing = 0
    for link in links:
        s, t = link["source"], link["target"]
        for n in (s, t):
            if n not in G:
                G.add_node(n, kind="requirement", name=n)      # a module that was not read
                missing += 1
        if G.has_edge(s, t):
            duplicates += 1
            continue
        G.add_edge(s, t, kind=link["type"])

    kinds: dict[str, int] = defaultdict(int)
    for _, _, d in G.edges(data=True):
        kinds[d["kind"]] += 1
    count = lambda kind: sum(1 for _, d in G.nodes(data=True) if d["kind"] == kind and d.get("module"))
    return G, {"nodes": G.number_of_nodes(), "edges": G.number_of_edges(),
               "modules": sum(1 for _, d in G.nodes(data=True) if d["kind"] == "module"),
               "requirements": count("requirement"), "headings": count("heading"),
               "information": count("information"), "linked_outside": missing,
               "duplicate_links": duplicates, "edges_by_kind": dict(sorted(kinds.items())),
               **graph_convention(convention)}


def code_names(units: Iterable[CodeUnit | dict]) -> dict[str, str]:
    """The names a document can use for a code element (lower case) -> its node in the
    code graph: "tracks.delete_track" always, "delete_track" when only one element has it
    (and it has an underscore), a unit when its name has a dot or an underscore."""
    qualified: dict[str, str] = {}
    simple: dict[str, set[str]] = defaultdict(set)
    for u in units:
        u = u if isinstance(u, CodeUnit) else CodeUnit(**u)
        if "." in u.unit or "_" in u.unit:
            qualified[u.unit] = u.unit
        for item in (*u.subprograms, *u.declarations, *u.types, *u.variables):
            node = f"{u.unit}.{item['name']}"
            qualified[node] = node
            if "_" in item["name"]:
                simple[item["name"]].add(node)
    return {**{name: next(iter(nodes)) for name, nodes in simple.items() if len(nodes) == 1}, **qualified}


def document_names(documents: Iterable[dict]) -> dict[str, str]:
    """unit -> the name of the document in the graph: its file name, or its path when two
    documents have the same file name (as in the citations of the index)."""
    documents = list(documents)
    count: dict[str, int] = defaultdict(int)
    for d in documents:
        count[Path(d["path"]).name] += 1
    return {d["unit"]: Path(d["path"]).name if count[Path(d["path"]).name] == 1 else d["path"] for d in documents}


def build_docs_graph(documents: Iterable[dict], names: dict[str, str] | None = None,
                     normalize: Callable[[str], str] = normalize_id) -> tuple[nx.DiGraph, dict]:
    """(graph, statistics) from the processed documents: each one a dict with `name` (as in
    document_names), `path`, `source`, `out_dir` and `structure` (its structure.json).
    `names` (code_names) are the code elements a section can mention."""
    names = names or {}
    G = nx.DiGraph()
    stats = {"documents": 0, "sections": 0, "requirements": 0, "mentions": 0}
    for d in documents:
        st = d["structure"]
        doc = f"doc:{d['name']}"
        G.add_node(doc, kind="document", name=d["name"], title=st.get("title", ""), path=d["path"],
                   source=d.get("source", ""), out_dir=d["out_dir"])
        stats["documents"] += 1
        for s in st.get("sections", []):
            sec = f"{doc}#{s['id']}"
            analysis = s.get("analysis") if isinstance(s.get("analysis"), dict) else {}
            reqs = {}
            for r in analysis.get("requirements") or []:
                rid = normalize(str(r.get("id") or "")) if isinstance(r, dict) else ""
                if rid and REQ_ID_SHAPE.match(rid) and rid not in reqs:
                    reqs[rid] = " ".join(str(r.get("text") or "").split())
            G.add_node(sec, kind="section", name=s.get("title", ""), document=d["name"], section=s["id"],
                       path=s.get("path", []), page_start=s.get("page_start") or 0, page_end=s.get("page_end") or 0,
                       file=str(Path(d["out_dir"]) / s["file"]) if s.get("file") else "",
                       requirements=[{"id": i, "text": t} for i, t in reqs.items()])
            G.add_edge(doc, sec, kind="contains")
            stats["sections"] += 1
            for rid in reqs:
                if rid not in G:
                    G.add_node(rid, kind="requirement", name=rid)
                G.add_edge(sec, rid, kind="declares")
            for code in sorted({names[t.lower()] for t in IDENTIFIER.findall(s.get("body") or "")
                                if t.lower() in names}):
                if code not in G:
                    G.add_node(code)            # the code graph gives it its kind; without it, it is dropped
                G.add_edge(sec, code, kind="mentions")
                stats["mentions"] += 1
    stats["requirements"] = sum(1 for _, d in G.nodes(data=True) if d.get("kind") == "requirement")
    return G, {"nodes": G.number_of_nodes(), "edges": G.number_of_edges(), **stats}
