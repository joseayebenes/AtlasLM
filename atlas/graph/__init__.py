"""Knowledge graph: code, DOORS requirements and documents in one graph, for traceability.

    build.py       build_code_graph: nodes and edges from the CodeUnits of every file
    sources.py     build_doors_graph, build_docs_graph: requirements and documents
    store.py       GraphStore: the graph in atlas.db, by origin; export to GraphML / JSON
    knowledge.py   KnowledgeGraph: queries (find_symbol, callers, impact, read_source...) and
                   traceability (trace, trace_code, matrix, gaps, neighbors, communities)
    tools.py       GraphTools: those queries as tools of agents and of the MCP server

    from atlas.graph import KnowledgeGraph
    g = KnowledgeGraph.from_config(cfg)
    g.impact("Delete_Track", depth=3)
    g.trace("SRS-0004")
    g.matrix("SRS_TMS")["gaps"]
"""
from __future__ import annotations

from atlas.graph.build import build_code_graph, find_communities
from atlas.graph.knowledge import KnowledgeGraph, display_attributes
from atlas.graph.sources import build_docs_graph, build_doors_graph, code_names, document_names
from atlas.graph.store import GraphStore, export_graph, to_mermaid
from atlas.graph.tools import GraphTools

__all__ = ["GraphStore", "GraphTools", "KnowledgeGraph", "build_code_graph", "build_docs_graph", "build_doors_graph",
           "code_names", "display_attributes", "document_names", "export_graph", "find_communities", "to_mermaid"]
