"""Knowledge graph. Phase 1: the code graph (legacy/ada_graph.py and ada_graph_mcp.py);
documents, DOORS and traceability join it in phase 4.

    build.py       build_code_graph: nodes and edges from the CodeUnits of every file
    store.py       GraphStore: the graph in atlas.db, by origin; export to GraphML / JSON
    knowledge.py   KnowledgeGraph: queries (find_symbol, callers, impact, read_source...)

    from atlas.graph import KnowledgeGraph
    g = KnowledgeGraph.from_config(cfg)
    g.impact("Delete_Track", depth=3)
    g.requirement("SRS-0004")
"""
from __future__ import annotations

from atlas.graph.build import build_code_graph
from atlas.graph.knowledge import KnowledgeGraph
from atlas.graph.store import GraphStore, export_graph

__all__ = ["GraphStore", "KnowledgeGraph", "build_code_graph", "export_graph"]
