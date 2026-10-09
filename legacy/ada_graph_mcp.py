#!/usr/bin/env python3
"""
ada_graph_mcp.py — Servidor MCP que expone el grafo de código Ada a un agente.

Uso:
    python ada_graph_mcp.py --graph grafo_ada.json [--root .]

    --graph  JSON generado por ada_graph.py
    --root   directorio desde el que se resuelven las rutas de fichero del grafo
             (el mismo desde el que se lanzó ada_graph.py)

Requisitos:
    pip install "mcp[cli]" networkx
Funciona con mcp 1.x (FastMCP) y mcp 2.x (MCPServer).

Prueba interactiva sin agente:
    mcp dev ada_graph_mcp.py            (abre el MCP Inspector en el navegador)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import networkx as nx

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------
# Carga del grafo
# --------------------------------------------------------------------------

GRAPH_PATH = Path(os.environ.get("ADA_GRAPH_JSON", "grafo_ada.json"))
ROOT = Path(os.environ.get("ADA_GRAPH_ROOT", "."))
if "--graph" in sys.argv or "--root" in sys.argv:
    _ap = argparse.ArgumentParser(add_help=False)
    _ap.add_argument("--graph", type=Path, default=GRAPH_PATH)
    _ap.add_argument("--root", type=Path, default=ROOT)
    _args, _ = _ap.parse_known_args()
    GRAPH_PATH, ROOT = _args.graph, _args.root

MAX_LIST = 40        # tope de elementos devueltos en listas
MAX_LINES = 300      # tope de líneas devueltas por lectura de fuente


def cargar(path: Path) -> tuple[nx.DiGraph, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    G = nx.DiGraph()
    for n in data["nodes"]:
        attrs = {k: v for k, v in n.items() if k != "id"}
        G.add_node(n["id"], **attrs)
    for e in data["edges"]:
        attrs = {k: v for k, v in e.items() if k not in ("source", "target")}
        G.add_edge(e["source"], e["target"], **attrs)
    return G, data.get("stats", {})


G, STATS = cargar(GRAPH_PATH)

mcp = FastMCP(
    "ada-graph",
    instructions=(
        "Grafo del código Ada del proyecto. Nodos: unit (paquete/fichero), subprogram, "
        "type, variable, constant, requirement, external. Aristas: defines, calls, "
        "converts (conversión de tipo), uses (acceso a global), withs, traces (requisito). "
        "Los identificadores van en minúsculas (Ada no distingue mayúsculas). "
        "Flujo recomendado: find_symbol -> get_node -> callers/callees/impact -> read_source."
    ),
)

# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def _norm(name: str) -> str:
    return name.strip().lower()


def _node(name: str) -> tuple[str, dict] | tuple[None, None]:
    n = _norm(name)
    if n in G:
        return n, G.nodes[n]
    simple = n.rsplit(".", 1)[-1]        # admite pkg.proc
    if simple in G:
        return simple, G.nodes[simple]
    return None, None


def _resumen(n: str) -> dict:
    d = G.nodes[n]
    out = {"id": n, "kind": d.get("kind")}
    if d.get("file"):
        out["file"] = d["file"]
    if d.get("start_line"):
        out["lines"] = f"{d['start_line']}-{d['end_line']}"
    elif d.get("line"):
        out["line"] = d["line"]
    doc = d.get("doc") or ""
    if doc:
        out["doc"] = doc.split("\n")[0][:120]
    if d.get("requirements"):
        out["requirements"] = d["requirements"].split(";")
    return out


def _lista(nodes, limit: int = MAX_LIST) -> dict:
    nodes = list(nodes)
    return {"count": len(nodes),
            "items": [_resumen(n) for n in nodes[:limit]],
            "truncated": len(nodes) > limit}


def _no_encontrado(name: str) -> dict:
    n = _norm(name).rsplit(".", 1)[-1]
    parecidos = [x for x in G.nodes if n in x][:10]
    return {"error": f"'{name}' no está en el grafo", "similar": parecidos}


def _leer_lineas(file: str, start: int, end: int) -> dict:
    path = Path(file)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        return {"error": f"no encuentro el fichero {path}; ¿--root correcto?"}
    lineas = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, start)
    end = min(len(lineas), end)
    truncated = False
    if end - start + 1 > MAX_LINES:
        end = start + MAX_LINES - 1
        truncated = True
    cuerpo = "\n".join(f"{i:5d} | {lineas[i - 1]}" for i in range(start, end + 1))
    return {"file": str(path), "start": start, "end": end, "truncated": truncated, "source": cuerpo}


# --------------------------------------------------------------------------
# Herramientas
# --------------------------------------------------------------------------

@mcp.tool()
def graph_stats() -> dict:
    """Resumen del grafo: número de ficheros, subprogramas, tipos, variables, requisitos."""
    kinds: dict[str, int] = {}
    for _, d in G.nodes(data=True):
        kinds[d.get("kind", "?")] = kinds.get(d.get("kind", "?"), 0) + 1
    return {"nodes": G.number_of_nodes(), "edges": G.number_of_edges(),
            "by_kind": kinds, "build_stats": STATS}


@mcp.tool()
def find_symbol(query: str, kind: str = "", limit: int = 20) -> dict:
    """Busca nodos cuyo nombre contiene `query` (sin distinguir mayúsculas).
    kind opcional: subprogram | type | variable | constant | unit | requirement | external."""
    q = _norm(query)
    hits = [n for n, d in G.nodes(data=True)
            if q in n and (not kind or d.get("kind") == kind)]
    hits.sort(key=lambda n: (not n.startswith(q), len(n), n))
    return _lista(hits, limit)


@mcp.tool()
def search_docs(text: str, limit: int = 20) -> dict:
    """Busca `text` en los comentarios de cabecera y en el fuente de tipos.
    Útil para localizar funcionalidad por descripción, no por nombre."""
    t = _norm(text)
    hits = [n for n, d in G.nodes(data=True)
            if t in (d.get("doc") or "").lower() or t in (d.get("source") or "").lower()]
    return _lista(hits, limit)


@mcp.tool()
def get_node(name: str) -> dict:
    """Ficha completa de un símbolo: tipo de nodo, fichero, líneas, comentario,
    requisitos trazados, y para tipos/variables el texto de su declaración."""
    n, d = _node(name)
    if n is None:
        return _no_encontrado(name)
    out = {"id": n, **{k: v for k, v in d.items()}}
    if out.get("requirements"):
        out["requirements"] = out["requirements"].split(";")
    out["callers_count"] = sum(1 for _, _, e in G.in_edges(n, data=True) if e.get("kind") == "calls")
    out["callees_count"] = sum(1 for _, _, e in G.out_edges(n, data=True) if e.get("kind") == "calls")
    unit = [u for u, _, e in G.in_edges(n, data=True) if e.get("kind") == "defines"]
    if unit:
        out["unit"] = unit[0]
    return out


@mcp.tool()
def callers(name: str, limit: int = MAX_LIST) -> dict:
    """Quién llama a este subprograma (aristas `calls` entrantes)."""
    n, _ = _node(name)
    if n is None:
        return _no_encontrado(name)
    return _lista((u for u, _, e in G.in_edges(n, data=True) if e.get("kind") == "calls"), limit)


@mcp.tool()
def callees(name: str, limit: int = MAX_LIST) -> dict:
    """A quién llama este subprograma, qué tipos convierte y qué globales usa."""
    n, _ = _node(name)
    if n is None:
        return _no_encontrado(name)
    out = {"calls": [], "converts": [], "uses": []}
    for _, v, e in G.out_edges(n, data=True):
        k = e.get("kind")
        if k in out:
            out[k].append(v)
    return {k: sorted(v)[:limit] for k, v in out.items()}


@mcp.tool()
def impact(name: str, depth: int = 3, limit: int = MAX_LIST) -> dict:
    """Análisis de impacto: todos los subprogramas que, directa o transitivamente,
    llaman a `name` hasta `depth` niveles. Úsalo antes de modificar algo."""
    n, _ = _node(name)
    if n is None:
        return _no_encontrado(name)
    calls_only = nx.DiGraph((u, v) for u, v, e in G.edges(data=True) if e.get("kind") == "calls")
    if n not in calls_only:
        return {"count": 0, "items": [], "truncated": False}
    niveles = nx.single_source_shortest_path_length(calls_only.reverse(copy=False), n, cutoff=depth)
    afectados = sorted((m for m in niveles if m != n), key=lambda m: (niveles[m], m))
    res = _lista(afectados, limit)
    for item in res["items"]:
        item["distance"] = niveles[item["id"]]
    return res


@mcp.tool()
def call_chain(source: str, target: str) -> dict:
    """Camino de llamadas más corto entre dos subprogramas, si existe."""
    a, _ = _node(source)
    b, _ = _node(target)
    if a is None:
        return _no_encontrado(source)
    if b is None:
        return _no_encontrado(target)
    calls_only = nx.DiGraph((u, v) for u, v, e in G.edges(data=True) if e.get("kind") == "calls")
    try:
        return {"path": nx.shortest_path(calls_only, a, b)}
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return {"path": None, "message": f"no hay cadena de llamadas de {a} a {b}"}


@mcp.tool()
def read_source(name: str, context: int = 0) -> dict:
    """Código fuente de un subprograma (con `context` líneas extra antes y después),
    o la declaración de un tipo/variable. Devuelve líneas numeradas."""
    n, d = _node(name)
    if n is None:
        return _no_encontrado(name)
    if d.get("kind") == "subprogram" and d.get("start_line"):
        return _leer_lineas(d["file"], d["start_line"] - context, d["end_line"] + context)
    if d.get("source"):
        return {"id": n, "file": d.get("file"), "line": d.get("line"), "source": d["source"]}
    if d.get("kind") == "unit" and d.get("file"):
        return _leer_lineas(d["file"], 1, MAX_LINES)
    return {"error": f"{n} ({d.get('kind')}) no tiene fuente asociado"}


@mcp.tool()
def read_file(file: str, start: int = 1, end: int = 200) -> dict:
    """Lee un rango de líneas de un fichero del proyecto (ruta relativa a --root)."""
    return _leer_lineas(file, start, end)


@mcp.tool()
def unit_info(name: str) -> dict:
    """Contenido de un paquete/fichero: qué subprogramas, tipos y variables define
    y qué unidades importa con `with`."""
    n, d = _node(name)
    if n is None or d.get("kind") != "unit":
        return _no_encontrado(name)
    defines = {"subprogram": [], "type": [], "variable": [], "constant": []}
    withs = []
    for _, v, e in G.out_edges(n, data=True):
        if e.get("kind") == "defines":
            defines.setdefault(G.nodes[v].get("kind", "?"), []).append(v)
        elif e.get("kind") == "withs":
            withs.append(v)
    return {"id": n, "file": d.get("file"),
            "defines": {k: sorted(v) for k, v in defines.items() if v},
            "withs": sorted(withs)}


@mcp.tool()
def type_users(name: str, limit: int = MAX_LIST) -> dict:
    """Subprogramas que convierten a este tipo y unidad que lo define."""
    n, d = _node(name)
    if n is None:
        return _no_encontrado(name)
    users = [u for u, _, e in G.in_edges(n, data=True) if e.get("kind") == "converts"]
    return {"type": n, "record": d.get("record"), "source": d.get("source"),
            "converts": _lista(users, limit)}


@mcp.tool()
def variable_users(name: str, limit: int = MAX_LIST) -> dict:
    """Subprogramas que acceden a esta variable/constante global."""
    n, _ = _node(name)
    if n is None:
        return _no_encontrado(name)
    users = [u for u, _, e in G.in_edges(n, data=True) if e.get("kind") == "uses"]
    return _lista(users, limit)


@mcp.tool()
def requirement(req_id: str, limit: int = MAX_LIST) -> dict:
    """Subprogramas y tipos trazados a un ID de requisito (p. ej. DELETE-X_2-MSG)."""
    r = req_id.strip()
    candidatos = [n for n, d in G.nodes(data=True) if d.get("kind") == "requirement" and n.upper() == r.upper()]
    if not candidatos:
        parecidos = [n for n, d in G.nodes(data=True)
                     if d.get("kind") == "requirement" and r.upper() in n.upper()][:10]
        return {"error": f"requisito '{req_id}' no encontrado", "similar": parecidos}
    r = candidatos[0]
    return {"requirement": r,
            "traced_by": _lista((u for u, _, e in G.in_edges(r, data=True) if e.get("kind") == "traces"), limit)}


@mcp.tool()
def list_requirements(prefix: str = "", limit: int = 100) -> dict:
    """Lista los requisitos del grafo (opcionalmente filtrados por prefijo) con
    cuántos elementos traza cada uno."""
    reqs = [(n, G.in_degree(n)) for n, d in G.nodes(data=True)
            if d.get("kind") == "requirement" and n.upper().startswith(prefix.upper())]
    reqs.sort(key=lambda kv: (-kv[1], kv[0]))
    return {"count": len(reqs), "items": [{"id": r, "traced": k} for r, k in reqs[:limit]],
            "truncated": len(reqs) > limit}


@mcp.tool()
def untraced(kind: str = "subprogram", limit: int = MAX_LIST) -> dict:
    """Elementos (subprogram | type) sin ningún requisito trazado."""
    nodes = [n for n, d in G.nodes(data=True) if d.get("kind") == kind and not d.get("requirements")]
    return _lista(nodes, limit)


if __name__ == "__main__":
    print(f"grafo cargado: {G.number_of_nodes()} nodos, {G.number_of_edges()} aristas "
          f"desde {GRAPH_PATH} (root={ROOT})", file=sys.stderr)
    mcp.run()
