"""Queries on the graph (the tools of legacy/ada_graph_mcp.py, as methods).

Every answer is a compact, JSON-serialisable dict, ready to be a tool of an agent or of
the MCP server: lists are capped (`limit`) and say whether they were `truncated`; a name
that is not found answers with `error` and `similar` names instead of raising.

A name can be given as its identifier ("tracks.update"), in any case, or by its simple
name ("Update") when only one element has it; if several do, the answer lists them.
Recommended flow: find_symbol -> node -> callers / callees / impact -> read_source.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import networkx as nx

from atlas.graph.store import GraphStore

__all__ = ["KnowledgeGraph"]

MAX_LIST = 40        # cap of the items of a list
MAX_LINES = 300      # cap of the lines of a source read


class KnowledgeGraph:
    def __init__(self, G: nx.DiGraph, root: str | Path = ".", stats: dict | None = None):
        self.G = G
        self.root = Path(root)          # the relative file paths of the graph start here
        self.build_stats = stats or {}
        self._lower = {n.lower(): n for n in G.nodes}
        self._by_name: dict[str, list[str]] = defaultdict(list)
        for n, d in G.nodes(data=True):
            if d.get("name") and d.get("kind") not in ("requirement", "external"):
                self._by_name[d["name"].lower()].append(n)
        self._calls = nx.DiGraph((u, v) for u, v, e in G.edges(data=True) if e.get("kind") == "calls")

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
        """Every subprogram that calls `name`, directly or transitively, up to `depth`
        levels, nearest first: what to review before changing it."""
        n, err = self.resolve(name)
        if err:
            return err
        if n not in self._calls:
            return {"count": 0, "items": [], "truncated": False}
        levels = nx.single_source_shortest_path_length(self._calls.reverse(copy=False), n, cutoff=depth)
        res = self.listing(sorted((m for m in levels if m != n), key=lambda m: (levels[m], m)), limit)
        for item in res["items"]:
            item["distance"] = levels[item["id"]]
        return res

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

    def requirement(self, req_id: str, limit: int = MAX_LIST) -> dict:
        """Elements traced to a requirement ID (any case)."""
        r = self._lower.get(req_id.strip().lower())
        if r is None or self.G.nodes[r].get("kind") != "requirement":
            key = req_id.strip().upper()
            return {"error": f"requirement '{req_id}' not found",
                    "similar": sorted(n for n, d in self.G.nodes(data=True)
                                      if d.get("kind") == "requirement" and key in n.upper())[:10]}
        return {"requirement": r, "traced_by": self.listing(self._edges(r, "traces", incoming=True), limit)}

    def list_requirements(self, prefix: str = "", limit: int = 100) -> dict:
        """Requirements of the graph (optionally by prefix), with how many elements trace each one."""
        reqs = sorted(((n, self.G.in_degree(n)) for n, d in self.G.nodes(data=True)
                       if d.get("kind") == "requirement" and n.upper().startswith(prefix.upper())),
                      key=lambda kv: (-kv[1], kv[0]))
        return {"count": len(reqs), "items": [{"id": r, "traced": k} for r, k in reqs[:limit]],
                "truncated": len(reqs) > limit}

    def untraced(self, kind: str = "subprogram", limit: int = MAX_LIST) -> dict:
        """Elements (subprogram | type) without any requirement."""
        return self.listing(sorted(n for n, d in self.G.nodes(data=True)
                                   if d.get("kind") == kind and not d.get("requirements")), limit)
