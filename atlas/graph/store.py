"""The graph in the workspace atlas.db, and its export.

Every loader writes its own part of the graph under an `origin` ("code", "doors", "docs")
and replaces it whole in one transaction. Loading merges every origin into one NetworkX
graph: a node that two origins share (a requirement found in the code and in DOORS) gets
the attributes of both, and the kind of the last origin that gives one (by name: DOORS
says whether an ID is a heading). A node that no loaded origin gives a kind to (the code
element a document mentions, when there is no code graph) is dropped with its edges.

    graph_nodes    origin, id, kind, data (JSON with the other attributes)
    graph_edges    origin, source, target, kind
    graph_builds   origin, statistics of its last build, date
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import networkx as nx

__all__ = ["GraphStore", "export_graph"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_nodes (origin TEXT, id TEXT, kind TEXT, data TEXT, PRIMARY KEY (origin, id));
CREATE TABLE IF NOT EXISTS graph_edges (origin TEXT, source TEXT, target TEXT, kind TEXT,
                                        PRIMARY KEY (origin, source, target, kind));
CREATE TABLE IF NOT EXISTS graph_builds (origin TEXT PRIMARY KEY, stats TEXT, built_at TEXT);
"""


class GraphStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def replace(self, origin: str, G: nx.DiGraph, stats: dict | None = None) -> None:
        with self.lock, self.db:
            self._delete(origin)
            self.db.executemany("INSERT INTO graph_nodes VALUES (?,?,?,?)",
                                [(origin, n, d.get("kind", ""), json.dumps({k: v for k, v in d.items() if k != "kind"},
                                                                           ensure_ascii=False))
                                 for n, d in G.nodes(data=True)])
            self.db.executemany("INSERT INTO graph_edges VALUES (?,?,?,?)",
                                [(origin, u, v, d.get("kind", "")) for u, v, d in G.edges(data=True)])
            self.db.execute("INSERT INTO graph_builds VALUES (?,?,?)",
                            (origin, json.dumps(stats or {}, ensure_ascii=False),
                             datetime.now().isoformat(timespec="microseconds")))     # tells two builds apart

    def _delete(self, origin: str) -> None:
        for table in ("graph_nodes", "graph_edges", "graph_builds"):
            self.db.execute(f"DELETE FROM {table} WHERE origin = ?", (origin,))

    def delete(self, origin: str) -> None:
        with self.lock, self.db:
            self._delete(origin)

    def load(self, origins: list[str] | None = None) -> nx.DiGraph:
        where, params = ("WHERE origin IN (%s)" % ",".join("?" * len(origins)), origins) if origins else ("", [])
        G = nx.DiGraph()
        with self.lock:
            for _, n, kind, data in self.db.execute(f"SELECT * FROM graph_nodes {where} ORDER BY origin, id", params):
                if n in G:
                    G.nodes[n].update({k: v for k, v in json.loads(data).items() if v not in ("", None, [], {})})
                    if kind:
                        G.nodes[n]["kind"] = kind
                else:
                    G.add_node(n, kind=kind, **json.loads(data))
            for _, u, v, kind in self.db.execute(f"SELECT * FROM graph_edges {where} ORDER BY origin, source, target",
                                                 params):
                G.add_edge(u, v, kind=kind)
        G.remove_nodes_from([n for n, d in G.nodes(data=True) if not d.get("kind")])
        return G

    def builds(self) -> dict[str, dict]:
        """Statistics and date of the last build of every origin."""
        with self.lock:
            return {o: {**json.loads(s), "built_at": at}
                    for o, s, at in self.db.execute("SELECT * FROM graph_builds ORDER BY origin")}

    def counts(self) -> dict[str, int]:
        with self.lock:
            return {"nodes": self.db.execute("SELECT COUNT(DISTINCT id) FROM graph_nodes").fetchone()[0],
                    "edges": self.db.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]}


def _scalar(v):
    if isinstance(v, list) and not any(isinstance(x, (list, dict)) for x in v):
        return ";".join(map(str, v))
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v


def export_graph(G: nx.DiGraph, prefix: str | Path, formats: list[str], stats: dict | None = None) -> list[Path]:
    """Writes `<prefix>.graphml` (Gephi, yEd, NetworkX) and/or `<prefix>.json` (nodes and
    edges, easy to give to a model). GraphML only takes scalars: a list of scalars becomes
    "a;b", and any other list or dict its JSON."""
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    written = []
    if "graphml" in formats:
        flat = nx.DiGraph()
        for n, d in G.nodes(data=True):
            flat.add_node(n, **{k: _scalar(v) for k, v in d.items()})
        flat.add_edges_from(G.edges(data=True))
        path = prefix.with_suffix(".graphml")
        nx.write_graphml(flat, path)
        written.append(path)
    if "json" in formats:
        path = prefix.with_suffix(".json")
        path.write_text(json.dumps({"stats": {k: v for k, v in (stats or {}).items() if not k.startswith("top_")},
                                    "nodes": [{"id": n, **d} for n, d in G.nodes(data=True)],
                                    "edges": [{"source": u, "target": v, **d} for u, v, d in G.edges(data=True)]},
                                   indent=1, ensure_ascii=False), encoding="utf-8")
        written.append(path)
    return written
