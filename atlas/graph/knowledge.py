"""Queries on the graph of code, requirements and documents (the tools of
legacy/ada_graph_mcp.py and the traceability of phase 4), as methods.

Every answer is a compact, JSON-serialisable dict, ready to be a tool of an agent or of
the MCP server: lists are capped (`limit`) and say whether they were `truncated`; a name
that is not found answers with `error` and `similar` names instead of raising.

A code element can be given as its identifier ("tracks.update"), in any case, or by its
simple name ("Update") when only one element has it; if several do, the answer lists them.
A requirement, by its ID in any case; a module, document or section, by its identifier
("module:SRS_TMS", "doc:hmi_manual.md", "doc:hmi_manual.md#003").

Code: find_symbol -> node -> callers / callees / impact -> read_source.
Traceability, without the model:
    trace(req)          text and attributes in DOORS, parent, children and links; the chain
                        of requirements it derives from; the sections that state it (and the
                        values that differ); code; tests; the test procedures that verify it
    trace_code(name)    requirements, callers, callees, tests and sections of an element
    impact(name)        what a change of a code element or of a requirement affects
    matrix(module)      requirement -> code -> tests -> procedures -> documents, and its gaps
    gaps(module)        requirements without code, tests, test procedure or derivation, code
                        without requirements, IDs that are not in DOORS or that DOORS links
                        in modules not read, values that differ between DOORS and documents
    neighbors(name)     the subgraph around a node, as JSON or Mermaid
    communities()       groups of code that work together (Louvain)

A test verifies a requirement when it cites it in its comments ("tag"), and covers it when
it calls, directly or not, code that implements it ("calls").

The links of DOORS mean what [sources.doors.convention] link_semantics says (it travels in
the statistics of the build of "doors"; see sources.graph_convention): a link of a
`derives` type makes its source a requirement derived from its target (an SRS that
satisfies an SSS); one of a `verifies` type makes its source a test procedure of its
target (an STP that verifies an SRS), which is not a test of the code; the `ignore` types
(references) are only shown as links.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from pathlib import Path

import networkx as nx

from atlas.graph.build import CODE_KINDS, find_communities
from atlas.graph.sources import graph_convention
from atlas.graph.store import GraphStore, to_mermaid

__all__ = ["KnowledgeGraph"]

MAX_LIST = 40        # cap of the items of a list
MAX_ANCESTORS = 5    # levels of the chain of trace up to the system requirement
MAX_LINES = 300      # cap of the lines of a source read
REQUIREMENT_KINDS = ("requirement", "heading", "information")
DEPENDS = ("calls", "converts", "uses")          # u depends on v: a change of v affects u
STRUCTURE = {"contains", "declares", "mentions", "defines", "withs", "implements", "verifies", *DEPENDS}
ID_LIKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)+\b")     # SRS-0001: not a value
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _natural(text: str) -> list:
    """Sort key that puts 1.9 before 1.10 and SRS-2 before SRS-10."""
    return [(0, int(x)) if x.isdigit() else (1, x) for x in re.split(r"(\d+)", text or "")]


def _values(text: str) -> set[str]:
    """The numbers of a requirement text (deadlines, limits...), without its IDs."""
    return {n.replace(",", ".") for n in NUMBER.findall(ID_LIKE.sub(" ", text or ""))}


class KnowledgeGraph:
    def __init__(self, G: nx.DiGraph, root: str | Path = ".", stats: dict | None = None,
                 convention: dict | None = None):
        """`convention`: link_semantics and top_modules ([sources.doors.convention]); by
        default, those of the build of DOORS (in `stats`)."""
        self.G = G
        self.root = Path(root)          # the relative file paths of the graph start here
        self.build_stats = stats or {}
        self.convention = graph_convention(convention if convention is not None
                                           else self.build_stats.get("doors"))
        self._lower = {n.lower(): n for n in G.nodes}
        self._by_name: dict[str, list[str]] = defaultdict(list)
        for n, d in G.nodes(data=True):
            if d.get("name") and d.get("kind") in CODE_KINDS:
                self._by_name[d["name"].lower()].append(n)
        self._calls = nx.DiGraph((u, v) for u, v, e in G.edges(data=True) if e.get("kind") == "calls")
        self._deps = nx.DiGraph((u, v) for u, v, e in G.edges(data=True) if e.get("kind") in DEPENDS)
        is_req = lambda n: G.nodes[n].get("kind") in REQUIREMENT_KINDS
        self._links = nx.DiGraph()        # DOORS links between requirements: source -> target
        self._tree = nx.DiGraph()         # DOORS hierarchy: parent -> child
        for u, v, e in G.edges(data=True):
            if is_req(u) and is_req(v):
                (self._tree if e.get("kind") == "contains" else self._links).add_edge(u, v, kind=e.get("kind"))
        semantics = self.convention["link_semantics"]
        self._verify_types = set(semantics["verifies"])
        derives, ignore = set(semantics["derives"]), set(semantics["ignore"])
        self._derives = nx.DiGraph()      # the links of a derives type: source derives from target
        self._derives.add_edges_from(
            (u, v, e) for u, v, e in self._links.edges(data=True)
            if e["kind"] not in self._verify_types and e["kind"] not in ignore and (not derives or e["kind"] in derives))
        # modules of test procedures: every link of theirs to another module verifies
        crossing: dict[str, set[bool]] = defaultdict(set)
        for u, v, e in self._links.edges(data=True):
            mu = G.nodes[u].get("module")
            if mu and G.nodes[v].get("module") != mu:
                crossing[mu].add(e["kind"] in self._verify_types)
        self._procedure_modules = {m for m, verifies in crossing.items() if verifies == {True}}
        self.doors = any(d.get("kind") == "module" for _, d in G.nodes(data=True))
        self._covering: dict[str, set[str]] | None = None
        self._groups: list[set[str]] | None = None

    @classmethod
    def open(cls, db: str | Path, root: str | Path = ".") -> "KnowledgeGraph":
        store = GraphStore(db)
        return cls(store.load(), root, store.builds())

    @classmethod
    def from_config(cls, cfg) -> "KnowledgeGraph":
        """The graph of <workspace>/atlas.db; file paths from the current directory, as the
        code roots of atlas.toml."""
        return cls.open(cfg.workspace / "atlas.db")

    # --- helpers --------------------------------------------------------------
    def resolve(self, name: str) -> tuple[str | None, dict | None]:
        """(identifier, None) or (None, error answer)."""
        key = name.strip().lower()
        if key in self._lower:
            return self._lower[key], None
        found = self._by_name.get(key.rsplit(".", 1)[-1], [])
        if "." in key:
            found = [n for n in found if n.lower().endswith("." + key)] or found
        if len(found) == 1:
            return found[0], None
        if found:
            return None, {"error": f"'{name}' is ambiguous", "candidates": sorted(found)[:20]}
        simple = key.rsplit(".", 1)[-1]
        return None, {"error": f"'{name}' is not in the graph", "similar": sorted(n for n in self.G if simple in n.lower())[:10]}

    def summary(self, n: str) -> dict:
        d = self.G.nodes[n]
        out = {"id": n, "kind": d.get("kind")}
        if d.get("kind") in REQUIREMENT_KINDS:
            return {**out, **{k: d[k] if k != "text" else d[k][:160] for k in ("module", "text") if d.get(k)}}
        if d.get("kind") == "section":
            return {**out, "document": d.get("document"), "section": d.get("section"), "title": d.get("name")}
        if d.get("kind") in ("document", "module"):
            return {**out, "title": d.get("title") or d.get("name")}
        if d.get("file"):
            out["file"] = d["file"]
        if d.get("start_line"):
            out["lines"] = f"{d['start_line']}-{d['end_line']}"
        elif d.get("line"):
            out["line"] = d["line"]
        if d.get("doc"):
            out["doc"] = d["doc"].split("\n")[0][:120]
        if d.get("requirements"):
            out["requirements"] = d["requirements"]
        return out

    def listing(self, nodes, limit: int = MAX_LIST) -> dict:
        nodes = list(nodes)
        return {"count": len(nodes), "items": [self.summary(n) for n in nodes[:limit]], "truncated": len(nodes) > limit}

    def _edges(self, n: str, kind: str, incoming: bool) -> list[str]:
        edges = self.G.in_edges(n, data=True) if incoming else self.G.out_edges(n, data=True)
        return sorted((u if incoming else v) for u, v, e in edges if e.get("kind") == kind)

    def _linked(self, n: str, kinds: set[str], incoming: bool) -> list[str]:
        edges = self.G.in_edges(n, data=True) if incoming else self.G.out_edges(n, data=True)
        return sorted({(u if incoming else v) for u, v, e in edges if e.get("kind") in kinds})

    def _kind(self, n: str) -> str:
        return self.G.nodes[n].get("kind", "")

    def _requirement(self, req_id: str) -> tuple[str | None, dict | None]:
        """(ID, None) or (None, error answer)."""
        key = "".join(req_id.split()).lower()
        r = self._lower.get(key)
        if r is not None and self._kind(r) in REQUIREMENT_KINDS:
            return r, None
        ids = [n for n, d in self.G.nodes(data=True) if d.get("kind") in REQUIREMENT_KINDS]
        similar = difflib.get_close_matches(key.upper(), ids, n=5, cutoff=0.8)        # SRS-004 -> SRS-0004
        similar += sorted(n for n in ids if key.upper() in n.upper() and n not in similar)
        return None, {"error": f"requirement '{req_id}' is not in the graph", "similar": similar[:10]}

    def _code_of(self, r: str) -> list[str]:
        return self._linked(r, {"implements"}, incoming=True)

    def _tests_of(self, r: str) -> list[str]:
        """The tests of the code that cite `r` (not the test procedures of DOORS)."""
        return [t for t in self._linked(r, {"verifies"}, incoming=True) if self._kind(t) == "test"]

    def _procedures_of(self, r: str) -> list[str]:
        """The requirements of DOORS (test procedures) linked to `r` with a verifies type."""
        if r not in self._links:
            return []
        return sorted({u for u, _, e in self._links.in_edges(r, data=True) if e["kind"] in self._verify_types},
                      key=_natural)

    def covering_tests(self, nodes) -> list[str]:
        """Tests that call, directly or not, any of these code elements (or convert to
        their type, or use their variable)."""
        if self._covering is None:      # once, from the tests: there are far fewer tests than elements
            covering: dict[str, set[str]] = defaultdict(set)
            for t, d in self.G.nodes(data=True):
                if d.get("kind") == "test" and t in self._deps:
                    for m in nx.descendants(self._deps, t):
                        covering[m].add(t)
            self._covering = covering
        return sorted({t for n in nodes for t in self._covering.get(n, ())})

    def _tests_for(self, r: str) -> list[dict]:
        tagged = self._tests_of(r)
        return [{"id": t, "how": "tag"} for t in tagged] + \
            [{"id": t, "how": "calls"} for t in self.covering_tests(self._code_of(r)) if t not in tagged]

    def _derived(self, r: str) -> list[str]:
        """The requirements of other modules linked to `r` with a derives type (the SRS that
        satisfy an SSS). Links inside a module (refinements) are not a level of derivation,
        and neither are references nor test procedures."""
        module = self.G.nodes[r].get("module")
        if not module or r not in self._derives:
            return []
        return sorted({u for u in self._derives.predecessors(r)
                       if self.G.nodes[u].get("module") not in ("", None, module)}, key=_natural)

    def _ancestors(self, r: str, depth: int = MAX_ANCESTORS) -> list[dict]:
        """The requirements `r` derives from in other modules (an SRS -> the SSS it
        satisfies), level after level up to `depth`: each one with the type of the link,
        its level, the requirement it is reached from (`via`) and a short text."""
        out, seen, frontier = [], {r}, [r]
        for level in range(1, depth + 1):
            following = []
            for m in frontier:
                if m not in self._derives:
                    continue
                module = self.G.nodes[m].get("module")
                for _, up, e in sorted(self._derives.out_edges(m, data=True), key=lambda e: _natural(e[1])):
                    d = self.G.nodes[up]
                    if up in seen or d.get("module") == module:
                        continue
                    seen.add(up)
                    following.append(up)
                    out.append({"id": up, "type": e["kind"], "level": level, "via": m,
                                **({"module": d["module"]} if d.get("module") else {"in_doors": False}),
                                "text": (d.get("text") or "")[:120]})
            frontier = following
        return out

    def _without_derivation(self, reqs: list[str]) -> list[str]:
        """The requirements with no link of a derives type to a requirement of another
        module, out of the top_modules and the modules of test procedures."""
        skip = set(self.convention["top_modules"]) | self._procedure_modules
        out = []
        for r in reqs:
            module = self.G.nodes[r].get("module")
            if not module or module in skip:
                continue
            ups = self._derives.successors(r) if r in self._derives else ()
            if not any(self.G.nodes[up].get("module") != module for up in ups):
                out.append(r)
        return out

    def _through(self, r: str) -> list[str]:
        """`r` and every requirement derived from it, level after level."""
        seen, todo = [r], [r]
        while todo:
            for d in self._derived(todo.pop()):
                if d not in seen:
                    seen.append(d)
                    todo.append(d)
        return seen

    def _section(self, s: str, requirement: str = "") -> dict:
        d = self.G.nodes[s]
        out = {"id": s, "document": d.get("document"), "section": d.get("section"), "title": d.get("name")}
        if requirement:
            text = next((x["text"] for x in d.get("requirements", []) if x["id"] == requirement), "")
            official = self.G.nodes[requirement].get("text", "") if self.G.nodes[requirement].get("module") else ""
            if text:
                out["text"] = text
            if text and official and _values(text) != _values(official):
                out["values_differ"] = {"doors": sorted(_values(official) - _values(text), key=_natural),
                                        "document": sorted(_values(text) - _values(official), key=_natural)}
        return out

    def _documents(self, r: str) -> list[dict]:
        return [self._section(s, r) for s in self._linked(r, {"declares"}, incoming=True)]

    def _module_requirements(self, module: str) -> tuple[list[str], dict | None]:
        """The requirements (not headings) of a DOORS module, in the order of the module;
        with no module, every requirement of the graph."""
        nodes = self.G.nodes
        if module:
            reqs = [n for n, d in nodes(data=True) if d.get("kind") == "requirement" and d.get("module") == module]
            if not reqs:
                modules = sorted(d["name"] for _, d in nodes(data=True) if d.get("kind") == "module")
                return [], {"error": f"module '{module}' is not in the graph", "modules": modules}
        else:
            reqs = [n for n, d in nodes(data=True) if d.get("kind") == "requirement"]
        return sorted(reqs, key=lambda n: (nodes[n].get("module", "~"), _natural(nodes[n].get("outline", "")),
                                           _natural(n))), None

    def _lines(self, file: str, start: int, end: int) -> dict:
        path = Path(file)
        if not path.is_absolute():
            path = self.root / path
        if not path.exists():
            return {"error": f"file {path} not found; is the root ({self.root}) right?"}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = max(1, start), min(len(lines), end)
        truncated = end - start + 1 > MAX_LINES
        if truncated:
            end = start + MAX_LINES - 1
        return {"file": str(path), "start": start, "end": end, "truncated": truncated,
                "source": "\n".join(f"{i:5d} | {lines[i - 1]}" for i in range(start, end + 1))}

    # --- queries --------------------------------------------------------------
    def stats(self) -> dict:
        """Size of the graph by kind of node, and the statistics of its builds."""
        kinds: dict[str, int] = defaultdict(int)
        for _, d in self.G.nodes(data=True):
            kinds[d.get("kind", "?")] += 1
        return {"nodes": self.G.number_of_nodes(), "edges": self.G.number_of_edges(), "by_kind": dict(kinds),
                "builds": self.build_stats}

    def find_symbol(self, query: str, kind: str = "", limit: int = 20) -> dict:
        """Nodes whose identifier contains `query` (any case). kind: subprogram | type |
        variable | constant | unit | requirement | external."""
        q = query.strip().lower()
        hits = [n for n, d in self.G.nodes(data=True) if q in n.lower() and (not kind or d.get("kind") == kind)]
        hits.sort(key=lambda n: (not self.G.nodes[n].get("name", n).lower().startswith(q), len(n), n))
        return self.listing(hits, limit)

    def search_docs(self, text: str, limit: int = 20) -> dict:
        """Nodes whose header comment or declaration source contains `text`: to find
        functionality by its description rather than by its name."""
        t = text.strip().lower()
        return self.listing([n for n, d in self.G.nodes(data=True)
                             if t in (d.get("doc") or "").lower() or t in (d.get("source") or "").lower()], limit)

    def node(self, name: str) -> dict:
        """Everything about an element: kind, file, lines, comment, requirements, the source
        of a type or variable, how many callers and callees, and the unit that defines it."""
        n, err = self.resolve(name)
        if err:
            return err
        out = {"id": n, **self.G.nodes[n]}
        out["callers_count"] = len(self._edges(n, "calls", incoming=True))
        out["callees_count"] = len(self._edges(n, "calls", incoming=False))
        unit = self._edges(n, "defines", incoming=True)
        if unit:
            out["unit"] = unit[0]
        return out

    def callers(self, name: str, limit: int = MAX_LIST) -> dict:
        """Who calls this subprogram."""
        n, err = self.resolve(name)
        return err or self.listing(self._edges(n, "calls", incoming=True), limit)

    def callees(self, name: str, limit: int = MAX_LIST) -> dict:
        """What this subprogram calls, which types it converts to and which globals it uses."""
        n, err = self.resolve(name)
        if err:
            return err
        return {k: self._edges(n, k, incoming=False)[:limit] for k in ("calls", "converts", "uses")}

    def impact(self, name: str, depth: int = 3, limit: int = MAX_LIST) -> dict:
        """What a change affects. Of a code element: every element that depends on it
        (calls it, converts to its type, uses its variable), directly or transitively, up
        to `depth` levels, nearest first; the requirements they implement and those these
        derive from in DOORS (satisfy, refine...); the tests that exercise it or verify those
        requirements, and the test procedures of DOORS that verify them; and the sections
        that state the requirements or mention the code. Of a requirement: the requirements
        that derive from it (its children and those linked to it with a derives type, up to
        `depth` levels), the code that implements them, their tests and procedures, and
        their sections."""
        n, err = self.resolve(name)
        if err:
            return err
        kind = self._kind(n)
        if kind in REQUIREMENT_KINDS:
            return self._requirement_impact(n, depth, limit)
        if kind not in CODE_KINDS:
            return {"error": f"{n} is a {kind}: impact takes a code element or a requirement"}
        levels = nx.single_source_shortest_path_length(self._deps.reverse(copy=False), n, cutoff=depth) \
            if n in self._deps else {n: 0}
        affected = sorted(levels, key=lambda m: (levels[m], m))
        res = {"node": n, "kind": kind, **self.listing([m for m in affected if m != n], limit)}
        for item in res["items"]:
            item["distance"] = levels[item["id"]]
        via: dict[str, list[str]] = defaultdict(list)      # requirement -> what makes it affected
        for m in affected:
            for r in self._linked(m, {"implements"}, incoming=False):
                via[r].append(m)
        for r in list(via):                                  # and those they satisfy, refine...
            for up in sorted(nx.descendants(self._derives, r), key=_natural) if r in self._derives else ():
                if up not in via:
                    via[up].append(r)
        tests = set(self.covering_tests([n]))
        for r in via:
            tests.update(self._tests_of(r))
        res["requirements"] = [{**self.summary(r), "via": v[:5]} for r, v in list(via.items())[:limit]]
        res["tests"] = sorted(tests)[:limit]
        res["procedures"] = sorted({p for r in via for p in self._procedures_of(r)}, key=_natural)[:limit]
        res["documents"] = self._affected_sections(via, affected, limit)
        return res

    def _requirement_impact(self, r: str, depth: int, limit: int) -> dict:
        derived = nx.compose(self._derives.reverse(copy=False), self._tree)  # an edge a -> b: b derives from a
        levels = nx.single_source_shortest_path_length(derived, r, cutoff=depth) if r in derived else {r: 0}
        reqs = sorted(levels, key=lambda m: (levels[m], _natural(m)))
        code = sorted({c for m in reqs for c in self._code_of(m)})
        tests = {t for m in reqs for t in self._tests_of(m)} | set(self.covering_tests(code))
        res = {"node": r, "kind": self._kind(r),
               "requirements": [{**self.summary(m), "distance": levels[m]} for m in reqs if m != r][:limit],
               **self.listing(code, limit), "tests": sorted(tests)[:limit],
               "procedures": sorted({p for m in reqs for p in self._procedures_of(m)}, key=_natural)[:limit],
               "documents": self._affected_sections({m: [] for m in reqs}, [], limit)}
        return res

    def _affected_sections(self, reqs, code, limit: int) -> list[dict]:
        found: dict[str, str] = {}
        for r in reqs:
            for s in self._linked(r, {"declares"}, incoming=True):
                found.setdefault(s, r)
        for c in code:
            for s in self._linked(c, {"mentions"}, incoming=True):
                found.setdefault(s, c)
        return [{**self._section(s), "via": v} for s, v in list(found.items())[:limit]]

    def call_chain(self, source: str, target: str) -> dict:
        """The shortest chain of calls from one subprogram to another, if there is one."""
        a, err = self.resolve(source)
        if err:
            return err
        b, err = self.resolve(target)
        if err:
            return err
        try:
            return {"path": nx.shortest_path(self._calls, a, b)}
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return {"path": None, "message": f"no chain of calls from {a} to {b}"}

    def read_source(self, name: str, context: int = 0) -> dict:
        """Numbered source of a subprogram (with `context` more lines around it), or the
        declaration of a type or variable."""
        n, err = self.resolve(name)
        if err:
            return err
        d = self.G.nodes[n]
        if d.get("kind") == "subprogram" and d.get("start_line"):
            return self._lines(d["file"], d["start_line"] - context, d["end_line"] + context)
        if d.get("source"):
            return {"id": n, "file": d.get("file"), "line": d.get("line"), "source": d["source"]}
        if d.get("kind") == "unit" and d.get("files"):
            return self._lines(d["files"][0], 1, MAX_LINES)
        return {"error": f"{n} ({d.get('kind')}) has no source"}

    def read_file(self, file: str, start: int = 1, end: int = 200) -> dict:
        """A range of lines of a project file (relative to the root)."""
        return self._lines(file, start, end)

    def unit_info(self, name: str) -> dict:
        """What a unit defines (subprograms, types, variables) and what it imports."""
        n, err = self.resolve(name)
        if err or self.G.nodes[n].get("kind") != "unit":
            return err or {"error": f"'{name}' is not a unit", "similar": []}
        defines: dict[str, list[str]] = defaultdict(list)
        for v in self._edges(n, "defines", incoming=False):
            defines[self.G.nodes[v].get("kind", "?")].append(v)
        return {"id": n, "files": self.G.nodes[n].get("files", []), "defines": dict(defines),
                "withs": self._edges(n, "withs", incoming=False)}

    def type_users(self, name: str, limit: int = MAX_LIST) -> dict:
        """Subprograms that convert to this type, and its declaration."""
        n, err = self.resolve(name)
        if err:
            return err
        d = self.G.nodes[n]
        return {"type": n, "record": d.get("record"), "source": d.get("source"),
                "converts": self.listing(self._edges(n, "converts", incoming=True), limit)}

    def variable_users(self, name: str, limit: int = MAX_LIST) -> dict:
        """Subprograms that access this package-level variable or constant."""
        n, err = self.resolve(name)
        return err or self.listing(self._edges(n, "uses", incoming=True), limit)

    def list_requirements(self, prefix: str = "", module: str = "", limit: int = 100) -> dict:
        """Requirements of the graph (optionally those whose ID starts with `prefix`, or
        those of a DOORS module), with how many code elements, tests and sections each one
        has."""
        reqs, err = self._module_requirements(module)
        if err:
            return err
        reqs = [r for r in reqs if r.upper().startswith(prefix.upper())]
        items = [{"id": r, **({"module": self.G.nodes[r]["module"]} if self.G.nodes[r].get("module") else {}),
                  "code": len(self._code_of(r)), "tests": len(self._tests_for(r)),
                  "documents": len(self._linked(r, {"declares"}, incoming=True))} for r in reqs[:limit]]
        return {"count": len(reqs), "items": items, "truncated": len(reqs) > limit}

    def untraced(self, kind: str = "subprogram", limit: int = MAX_LIST) -> dict:
        """Elements (subprogram | type) without any requirement."""
        return self.listing(sorted(n for n, d in self.G.nodes(data=True)
                                   if d.get("kind") == kind and not d.get("requirements")), limit)

    # --- traceability -----------------------------------------------------------
    def trace(self, req_id: str, limit: int = MAX_LIST) -> dict:
        """Everything about a requirement: its text and attributes in DOORS, its parent,
        children and links; the requirements it derives from in other modules, up to the
        system requirement (`ancestors`: up to 5 levels, with the type of each link); the
        document sections that state it, with their text and the values (deadlines,
        limits) that differ from DOORS; the code that implements it; the
        tests of the code that verify it (citing it, "tag") or cover it (calling that code,
        "calls"); and the test procedures of DOORS that verify it (a link of a verifies type)."""
        r, err = self._requirement(req_id)
        if err:
            return err
        d = self.G.nodes[r]
        out = {"id": r, "kind": d["kind"]}
        if self.doors:
            out["in_doors"] = bool(d.get("module"))
        out.update({k: d[k] for k in ("module", "text", "attributes", "outline") if d.get(k)})
        parents = [p for p in self._tree.predecessors(r)] if r in self._tree else []
        links = lambda edges, end: sorted(({"id": e[end], "type": e[2]["kind"]} for e in edges),
                                          key=lambda x: (_natural(x["id"]), x["type"]))
        out.update({
            "parent": parents[0] if parents else (d.get("parent") or None),
            "children": sorted(self._tree.successors(r), key=_natural)[:limit] if r in self._tree else [],
            "links_out": links(self._links.out_edges(r, data=True), 1)[:limit] if r in self._links else [],
            "links_in": links(self._links.in_edges(r, data=True), 0)[:limit] if r in self._links else [],
            "ancestors": self._ancestors(r)[:limit],
            "documents": self._documents(r)[:limit],
            "implemented_by": self.listing(self._code_of(r), limit),
            "verified_by": self._tests_for(r)[:limit],
            "procedures": [{"id": u, "type": e["kind"], "text": (self.G.nodes[u].get("text") or "")[:120]}
                           for u, _, e in sorted(self._links.in_edges(r, data=True), key=lambda e: _natural(e[0]))
                           if e["kind"] in self._verify_types][:limit] if r in self._links else [],
        })
        return out

    def trace_code(self, name: str, limit: int = MAX_LIST) -> dict:
        """Traceability of a code element: the requirements it implements (or verifies, if
        it is a test), who calls it and what it calls, the tests that exercise it (or, for
        a test, the code it exercises and the requirements it covers) and the sections that
        mention it or state its requirements. Of a unit, that of everything it defines."""
        n, err = self.resolve(name)
        if err:
            return err
        kind = self._kind(n)
        if kind not in CODE_KINDS:
            return {"error": f"{n} is a {kind}, not a code element" +
                    ("; use trace for a requirement" if kind in REQUIREMENT_KINDS else "")}
        elements = [n] + (self._edges(n, "defines", incoming=False) if kind == "unit" else [])
        reqs = sorted({r for m in elements for r in self._linked(m, {"implements", "verifies"}, incoming=False)},
                      key=_natural)
        out = {**self.summary(n), "requirements": [self.summary(r) for r in reqs[:limit]]}
        if kind != "unit":
            out["callers"] = self._edges(n, "calls", incoming=True)[:limit]
            out["callees"] = self.callees(n, limit)
        if kind == "test":
            exercised = sorted(m for m in nx.descendants(self._deps, n) if self._kind(m) != "test") \
                if n in self._deps else []
            covered = sorted({r for m in exercised for r in self._linked(m, {"implements"}, incoming=False)},
                             key=_natural)
            out["exercises"] = exercised[:limit]
            out["covers"] = covered[:limit]
        else:
            out["tests"] = self.covering_tests(elements)[:limit]
        found = {s: "mentions" for m in elements for s in self._linked(m, {"mentions"}, incoming=True)}
        for r in reqs:
            for s in self._linked(r, {"declares"}, incoming=True):
                found.setdefault(s, r)
        out["documents"] = [{**self._section(s), "via": v} for s, v in list(found.items())[:limit]]
        return out

    def matrix(self, module: str = "", limit: int = 50, offset: int = 0) -> dict:
        """Traceability matrix of a DOORS module (with no module, of every requirement in
        the graph): for each requirement, the requirements of other modules derived from it,
        its code, the tests of the code (citing it, or covering it by calling that code),
        the test procedures of DOORS that verify it and the sections that state it; a
        summary; and the gaps, the requirements without code, without tests and without a
        test procedure. A requirement whose derived requirements have code (an SSS through
        its SRS) is implemented through them, and the same with tests and procedures.
        `limit` and `offset` page the rows; the gaps are always complete (up to 200 IDs each)."""
        reqs, err = self._module_requirements(module)
        if err:
            return err
        rows = [self._row(r) for r in reqs]
        no_code, no_tests = self._without_code(reqs), self._without_tests(reqs)
        no_procedure = self._without_procedure(reqs)
        gap = lambda ids: {"count": len(ids), "items": ids[:200], "truncated": len(ids) > 200}
        return {"module": module or "(all)",
                "summary": {"requirements": len(rows), "with_code": len(rows) - len(no_code),
                            "with_tests": len(rows) - len(no_tests),
                            "with_procedures": len(rows) - len(no_procedure),
                            "with_documents": sum(1 for r in rows if r["documents"])},
                "rows": rows[offset:offset + limit], "offset": offset, "truncated": offset + limit < len(rows),
                "gaps": {"without_code": gap(no_code), "without_tests": gap(no_tests),
                         "without_test_procedure": gap(no_procedure)}}

    def _row(self, r: str) -> dict:
        d = self.G.nodes[r]
        attributes = {k.lower(): v for k, v in (d.get("attributes") or {}).items()}
        tests = self._tests_for(r)
        return {"id": r, "text": (d.get("text") or "")[:120],
                **{k: str(attributes[k]) for k in ("status", "verification") if attributes.get(k)},
                "derived": self._derived(r), "code": self._code_of(r),
                "tests": [t["id"] for t in tests if t["how"] == "tag"],
                "covering_tests": [t["id"] for t in tests if t["how"] == "calls"],
                "procedures": self._procedures_of(r),
                "documents": [f"{x['document']} §{x['title']}" for x in self._documents(r)]}

    def _without_code(self, reqs: list[str]) -> list[str]:
        return [r for r in reqs if not any(self._code_of(x) for x in self._through(r))]

    def _without_tests(self, reqs: list[str]) -> list[str]:
        return [r for r in reqs if not any(self._tests_for(x) for x in self._through(r))]

    def _without_procedure(self, reqs: list[str]) -> list[str]:
        return [r for r in reqs if not any(self._procedures_of(x) for x in self._through(r))]

    def gaps(self, module: str = "", limit: int = MAX_LIST) -> dict:
        """What traceability lacks: the requirements (of a DOORS module, or all) without
        code, without tests of the code or without a test procedure of DOORS, neither their
        own nor through the requirements derived from them; the requirements that derive
        from nothing in another module (by module; neither those of [sources.doors.convention]
        top_modules nor test procedures); the subprograms that cite no requirement; the IDs
        cited in the code or stated in the documents that are not in DOORS, and those that
        DOORS links in modules that were not read (who links them, with what type); and the
        requirements whose values (deadlines, limits) differ between DOORS and a document."""
        reqs, err = self._module_requirements(module)
        if err:
            return err
        if self.doors and not module:
            reqs = [r for r in reqs if self.G.nodes[r].get("module")]
        part = lambda items: {"count": len(items), "items": items[:limit], "truncated": len(items) > limit}
        out = {"module": module or "(all)",
               "requirements_without_code": part(self._without_code(reqs)),
               "requirements_without_tests": part(self._without_tests(reqs)),
               "requirements_without_test_procedure": part(self._without_procedure(reqs)),
               **({"requirements_without_derivation": self._derivation_gap(reqs, part)} if self.doors else {}),
               "code_without_requirement": part(sorted(n for n, d in self.G.nodes(data=True)
                                                       if d.get("kind") == "subprogram"
                                                       and not self._linked(n, {"implements"}, incoming=False)))}
        if self.doors:
            outside = sorted((n for n, d in self.G.nodes(data=True)
                              if d.get("kind") == "requirement" and not d.get("module")), key=_natural)
            out["cited_in_code_not_in_doors"] = part(
                [{"id": r, "cited_by": self._linked(r, {"implements", "verifies"}, incoming=True)[:5]}
                 for r in outside if self._linked(r, {"implements", "verifies"}, incoming=True)])
            out["stated_in_documents_not_in_doors"] = part(
                [{"id": r, "documents": [f"{x['document']} §{x['title']}" for x in self._documents(r)][:5]}
                 for r in outside if self._linked(r, {"declares"}, incoming=True)])
            linked = []
            for r in outside:
                by = [{"id": u, "type": e["kind"]}
                      for u, _, e in sorted(self._links.in_edges(r, data=True), key=lambda e: (_natural(e[0]), e[2]["kind"]))
                      if not module or self.G.nodes[u].get("module") == module] if r in self._links else []
                if by:
                    linked.append({"id": r, "linked_by": by[:5]})
            out["linked_to_modules_not_read"] = part(linked)
        differ = []
        for r in reqs:
            for x in self._documents(r):
                if "values_differ" in x:
                    differ.append({"id": r, "section": f"{x['document']} §{x['title']}", **x["values_differ"]})
        out["values_differ"] = part(differ)
        return out

    def _derivation_gap(self, reqs: list[str], part) -> dict:
        found = self._without_derivation(reqs)
        by_module = Counter(self.G.nodes[r]["module"] for r in found)
        return {**part(found), "by_module": dict(sorted(by_module.items()))}

    def neighbors(self, name: str, depth: int = 1, kinds: str = "", limit: int = 60, format: str = "json") -> dict:
        """The subgraph around a node (code element, requirement, module, document or
        section), up to `depth` steps in either direction, nearest first. `kinds` keeps only
        some kinds of edge ("calls,implements"). format: json (nodes and edges) | mermaid
        (a diagram to paste in Markdown)."""
        n, err = self.resolve(name)
        if err:
            return err
        if format not in ("json", "mermaid"):
            return {"error": f"unknown format '{format}' (json | mermaid)"}
        allowed = {k.strip() for k in kinds.split(",") if k.strip()}
        keep = lambda e: not allowed or e.get("kind") in allowed
        levels, frontier, truncated = {n: 0}, [n], False
        for level in range(1, depth + 1):
            nxt = []
            for m in frontier:
                around = sorted({v for _, v, e in self.G.out_edges(m, data=True) if keep(e)} |
                                {u for u, _, e in self.G.in_edges(m, data=True) if keep(e)})
                for v in around:
                    if v in levels:
                        continue
                    if len(levels) >= limit:
                        truncated = True
                        break
                    levels[v] = level
                    nxt.append(v)
            frontier = nxt
        sub = nx.DiGraph()
        sub.add_nodes_from((m, self.G.nodes[m]) for m in levels)
        sub.add_edges_from((u, v, e) for u, v, e in self.G.subgraph(levels).edges(data=True) if keep(e))
        if format == "mermaid":
            return {"center": n, "nodes": sub.number_of_nodes(), "truncated": truncated,
                    "mermaid": to_mermaid(sub, center=n)}
        return {"center": n,
                "nodes": [{**self.summary(m), "distance": levels[m]}
                          for m in sorted(levels, key=lambda m: (levels[m], m))],
                "edges": [{"source": u, "target": v, "kind": e.get("kind")} for u, v, e in sub.edges(data=True)],
                "truncated": truncated}

    def communities(self, limit: int = 20) -> dict:
        """Groups of code that work together (Louvain over calls, uses, withs and what each
        unit defines), largest first: their units, their most connected elements and the
        requirements they implement. Computed in the build with [graph] communities = true,
        or here on first use."""
        if self._groups is None:
            stored = {n: d["community"] for n, d in self.G.nodes(data=True) if d.get("community") is not None}
            ids = stored or find_communities(self.G)
            groups: dict[int, set[str]] = defaultdict(set)
            for node, group in ids.items():
                if node in self.G:
                    groups[group].add(node)
            self._groups = [groups[g] for g in sorted(groups)]
        items = []
        for i, members in enumerate(self._groups[:limit]):
            units = Counter(self.G.nodes[m].get("unit") or m for m in members if self._kind(m) != "unit")
            reqs = Counter(r for m in members for r in self._linked(m, {"implements"}, incoming=False))
            top = sorted((m for m in members if self._kind(m) != "unit"), key=lambda m: (-self.G.degree(m), m))
            items.append({"id": i, "size": len(members), "units": [u for u, _ in units.most_common(5)],
                          "members": top[:10], "requirements": [r for r, _ in reqs.most_common(10)]})
        return {"count": len(self._groups), "items": items, "truncated": len(self._groups) > limit}
