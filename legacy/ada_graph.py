#!/usr/bin/env python3
"""
ada_graph.py — Construye un grafo de código Ada con tree-sitter y NetworkX.

Uso:
    python ada_graph.py <directorio_fuente> [--out grafo_ada] [--req-pattern REGEX]

Genera:
    <out>.graphml   grafo para Gephi / NetworkX
    <out>.json      nodos y aristas en JSON (fácil de exponer a un LLM)

Cada subprograma lleva: fichero, líneas de inicio/fin, comentario de cabecera
(preferentemente el del .ads) y los IDs de requisito encontrados en sus
comentarios, que además se enlazan como nodos "requirement" con aristas "traces".

Requisitos:
    pip install tree-sitter tree-sitter-ada networkx
Compatible con py-tree-sitter 0.23 a 0.25.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import networkx as nx
from tree_sitter import Language, Parser, Query
import tree_sitter_ada as tsada

try:  # py-tree-sitter >= 0.25
    from tree_sitter import QueryCursor
except ImportError:  # 0.23 / 0.24
    QueryCursor = None

EXTENSIONES = {".ads", ".adb", ".ada"}

# Formato de trazabilidad en comentarios:   -- Req: DELETE-X_2-MSG
# Se captura lo que sigue a "Req:" (se admiten varios IDs separados por coma o
# espacio). Ajustable con --req-pattern; si la regex tiene un grupo, se usa el grupo 1.
REQ_PATTERN_DEFAULT = r"(?i)\bReq\s*:\s*([^\n]+)"
# Un ID debe llevar al menos un separador (-, _, .) o un dígito: así no se cuelan palabras sueltas
REQ_ID_SHAPE = re.compile(r"^(?=.*[-_.\d])[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*$")

LANG = Language(tsada.language())
PARSER = Parser(LANG)

# Cada patrón se compila por separado: si la gramática instalada usa otro
# nombre de nodo, se avisa y se sigue con los que sí compilan.
DEF_PATTERNS = [   # cuerpos (normalmente en .adb)
    "(subprogram_body (procedure_specification name: (identifier) @name)) @body",
    "(subprogram_body (function_specification name: (identifier) @name)) @body",
]
DECL_PATTERNS = [  # declaraciones (normalmente en .ads): de aquí sale la documentación
    "(subprogram_declaration (procedure_specification name: (identifier) @name)) @decl",
    "(subprogram_declaration (function_specification name: (identifier) @name)) @decl",
]
CALL_PATTERNS = [
    "(procedure_call_statement (identifier) @call)",
    "(function_call (identifier) @call)",
    "(procedure_call_statement (selected_component) @call)",
    "(function_call (selected_component) @call)",
]
WITH_PATTERNS = [
    "(with_clause (identifier) @with)",
    "(with_clause (selected_component) @with)",
]
COMMENT_PATTERNS = ["(comment) @c"]
TYPE_PATTERNS = [   # tipos y subtipos (el nombre es el primer identifier hijo)
    "(full_type_declaration) @decl",
    "(subtype_declaration) @decl",
]
VAR_PATTERNS = [    # objetos declarados a nivel de paquete (se filtran los locales)
    "(object_declaration name: (identifier) @name) @decl",
]
LOCAL_PATTERNS = [  # nombres locales de un cuerpo: `Local (1)` es indexación, no llamada
    "(object_declaration name: (identifier) @local)",
    "(parameter_specification) @param",
]

# En Ada, `Nombre (X)` es sintácticamente igual para llamada, conversión de tipo
# e indexación de array. Los tipos predefinidos nunca son llamadas:
PREDEFINED_TYPES = {
    "integer", "natural", "positive", "float", "long_float", "long_long_float",
    "short_float", "long_integer", "long_long_integer", "short_integer",
    "short_short_integer", "boolean", "character", "wide_character",
    "wide_wide_character", "string", "wide_string", "wide_wide_string", "duration",
    "unsigned_8", "unsigned_16", "unsigned_32", "unsigned_64",
    "integer_8", "integer_16", "integer_32", "integer_64",
}
MAX_SOURCE_CHARS = 2000  # tope para el texto de una declaración de tipo guardado en el nodo


def compilar(patrones: list[str], etiqueta: str) -> Query | None:
    """Compila los patrones que la gramática acepta y descarta el resto."""
    validos = []
    for p in patrones:
        try:
            Query(LANG, p)
            validos.append(p)
        except Exception as e:  # nombre de nodo/campo inexistente
            print(f"[aviso] patrón {etiqueta} descartado: {p}\n         -> {e}", file=sys.stderr)
    if not validos:
        print(f"[error] ningún patrón de {etiqueta} compila", file=sys.stderr)
        return None
    return Query(LANG, "\n".join(validos))


DEF_QUERY = compilar(DEF_PATTERNS, "cuerpo")
DECL_QUERY = compilar(DECL_PATTERNS, "declaración")
CALL_QUERY = compilar(CALL_PATTERNS, "llamada")
WITH_QUERY = compilar(WITH_PATTERNS, "with")
COMMENT_QUERY = compilar(COMMENT_PATTERNS, "comentario")
TYPE_QUERY = compilar(TYPE_PATTERNS, "tipo")
VAR_QUERY = compilar(VAR_PATTERNS, "variable")
LOCAL_QUERY = compilar(LOCAL_PATTERNS, "local")


def captures(query: Query, node) -> dict:
    if QueryCursor is not None:
        return QueryCursor(query).captures(node)
    return query.captures(node)


def matches(query: Query, node) -> list:
    if QueryCursor is not None:
        return QueryCursor(query).matches(node)
    return query.matches(node)


def texto(node, src: bytes) -> str:
    """Texto de un nodo cortando el fuente a mano. No usa node.text, que en
    algunas versiones del binding provoca un access violation en Windows."""
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def norm(nombre: str) -> str:
    """Ada no distingue mayúsculas: normaliza para que las aristas casen."""
    return nombre.strip().lower()


# --------------------------------------------------------------------------
# Comentarios y requisitos
# --------------------------------------------------------------------------

def limpiar_comentario(linea: str) -> str | None:
    """Quita el '--' inicial; descarta líneas decorativas (-----, ====)."""
    s = re.sub(r"^\s*-{2,}\s?", "", linea).rstrip()
    if re.fullmatch(r"[-=*#_\s]*", s):
        return None
    return s


def comentario_previo(node, lineas_fuente: list[str]) -> str:
    """Bloque de '--' contiguo justo encima de la línea donde empieza el nodo.
    Tolera una línea en blanco entre el comentario y el código."""
    i = node.start_point[0] - 1
    bloque = []
    blancas = 0
    while i >= 0:
        l = lineas_fuente[i].strip()
        if l.startswith("--"):
            bloque.append(l)
            blancas = 0
        elif l == "" and blancas == 0 and not bloque:
            blancas += 1
        else:
            break
        i -= 1
    bloque.reverse()
    limpias = [c for c in (limpiar_comentario(x) for x in bloque) if c is not None]
    return "\n".join(limpias).strip()


def comentarios_internos(node, src: bytes) -> str:
    """Todos los comentarios dentro de un nodo (para buscar IDs de requisito)."""
    if not COMMENT_QUERY:
        return ""
    return "\n".join(texto(c, src) for c in captures(COMMENT_QUERY, node).get("c", []))


def extraer_reqs(texto_fuente: str, patron: re.Pattern) -> list[str]:
    """IDs de requisito. Tras 'Req:' se leen tokens con forma de ID hasta que
    aparece uno que no lo parece (así 'Req: A-B, C-D borra mensajes' -> A-B, C-D)."""
    ids = set()
    for m in patron.finditer(texto_fuente):
        resto = m.group(1) if m.groups() else m.group(0)
        for tok in re.split(r"[,\s;]+", resto.strip()):
            tok = tok.strip(".:;,()[]")
            if not tok:
                continue
            if not REQ_ID_SHAPE.match(tok):
                break
            ids.add(tok)
    return sorted(ids)


def dentro_de_subprograma(node) -> bool:
    p = node.parent
    while p is not None:
        if p.type == "subprogram_body":
            return True
        p = p.parent
    return False


def nombres_parametro(param):
    """Identificadores de un parameter_specification excluyendo el tipo (subtype_mark)."""
    out = []
    for i, c in enumerate(param.children):
        if c.type == "identifier" and param.field_name_for_child(i) != "subtype_mark":
            out.append(c)
    return out


def primer_identificador(node):
    for c in node.children:
        if c.type == "identifier":
            return c
    return None


# --------------------------------------------------------------------------
# Análisis por fichero
# --------------------------------------------------------------------------

def analizar(path: Path, patron_req: re.Pattern) -> dict:
    src = path.read_bytes()
    lineas_fuente = src.decode("utf-8", errors="replace").splitlines()
    tree = PARSER.parse(src)
    root = tree.root_node

    withs = []
    if WITH_QUERY:
        withs = sorted({norm(texto(n, src)) for n in captures(WITH_QUERY, root).get("with", [])})

    # Declaraciones (spec): nombre -> doc, requisitos, línea
    decls = []
    if DECL_QUERY:
        for _, caps in matches(DECL_QUERY, root):
            decl = caps["decl"][0]
            doc = comentario_previo(decl, lineas_fuente)
            decls.append({
                "name": norm(texto(caps["name"][0], src)),
                "line": decl.start_point[0] + 1,
                "doc": doc,
                "reqs": extraer_reqs(doc + "\n" + texto(decl, src), patron_req),
            })

    # Tipos y subtipos (con su fuente: en un record son los campos)
    tipos = []
    if TYPE_QUERY:
        for decl in captures(TYPE_QUERY, root).get("decl", []):
            ident = primer_identificador(decl)
            if ident is None:
                continue
            fuente = texto(decl, src)
            doc_t = comentario_previo(decl, lineas_fuente)
            tipos.append({
                "name": norm(texto(ident, src)),
                "line": decl.start_point[0] + 1,
                "doc": doc_t,
                "reqs": extraer_reqs(doc_t, patron_req),
                "source": fuente[:MAX_SOURCE_CHARS],
                "record": "record" in fuente.lower(),
                "local": dentro_de_subprograma(decl),
            })

    # Variables/constantes a nivel de paquete (las locales se descartan)
    variables = []
    if VAR_QUERY:
        for _, caps in matches(VAR_QUERY, root):
            decl = caps["decl"][0]
            if dentro_de_subprograma(decl):
                continue
            fuente = texto(decl, src)
            for ident in caps["name"]:
                variables.append({
                    "name": norm(texto(ident, src)),
                    "line": decl.start_point[0] + 1,
                    "constant": " constant " in f" {fuente.lower()} ",
                    "source": fuente[:300],
                })

    # Cuerpos con su rango de bytes
    cuerpos = []
    if DEF_QUERY:
        for _, caps in matches(DEF_QUERY, root):
            body = caps["body"][0]
            cuerpos.append({
                "name": norm(texto(caps["name"][0], src)),
                "start_line": body.start_point[0] + 1,
                "end_line": body.end_point[0] + 1,
                "start": body.start_byte,
                "end": body.end_byte,
                "node": body,
            })

    # Para cada cuerpo: llamadas propias (no de subprogramas anidados),
    # comentario de cabecera y requisitos citados en sus comentarios
    subprogramas = []
    for c in cuerpos:
        anidados = [(o["start"], o["end"]) for o in cuerpos
                    if o is not c and o["start"] >= c["start"] and o["end"] <= c["end"]]
        locales = set()
        if LOCAL_QUERY:
            caps_l = captures(LOCAL_QUERY, c["node"])
            locales = {norm(texto(n, src)) for n in caps_l.get("local", [])}
            for param in caps_l.get("param", []):
                locales.update(norm(texto(n, src)) for n in nombres_parametro(param))
        calls = set()
        if CALL_QUERY:
            for n in captures(CALL_QUERY, c["node"]).get("call", []):
                if any(s <= n.start_byte < e for s, e in anidados):
                    continue
                nombre = norm(texto(n, src))
                if nombre in locales:          # indexación de array/parámetro local
                    continue
                calls.add(nombre)
        doc = comentario_previo(c["node"], lineas_fuente)
        reqs = extraer_reqs(doc + "\n" + comentarios_internos(c["node"], src), patron_req)
        subprogramas.append({
            "name": c["name"],
            "start_line": c["start_line"],
            "end_line": c["end_line"],
            "calls": sorted(calls),
            "doc": doc,
            "reqs": reqs,
        })

    return {"file": str(path), "unit": norm(path.stem), "withs": withs,
            "decls": decls, "subprogramas": subprogramas,
            "tipos": tipos, "variables": variables}


# --------------------------------------------------------------------------
# Grafo
# --------------------------------------------------------------------------

def construir_grafo(raiz: Path, patron_req: re.Pattern, debug: bool = False) -> tuple[nx.DiGraph, dict]:
    G = nx.DiGraph()
    ficheros = sorted(p for p in raiz.rglob("*") if p.suffix.lower() in EXTENSIONES)
    print(f"{len(ficheros)} ficheros Ada encontrados")

    infos = []
    for p in ficheros:
        try:
            infos.append(analizar(p, patron_req))
        except Exception as e:
            if debug:
                raise
            print(f"[error] {p}: {e}", file=sys.stderr)

    # Documentación y requisitos declarados en specs (.ads), indexados por nombre
    spec_info: dict[str, dict] = {}
    for info in infos:
        for d in info["decls"]:
            spec_info.setdefault(d["name"], {"doc": "", "reqs": set(), "spec_file": info["file"]})
            if d["doc"] and not spec_info[d["name"]]["doc"]:
                spec_info[d["name"]]["doc"] = d["doc"]
            spec_info[d["name"]]["reqs"].update(d["reqs"])

    # Pasada 1: nodos definidos (unidades, subprogramas, tipos, variables)
    definidos: dict[str, str] = {}
    tipos_def: set[str] = set()
    vars_def: set[str] = set()
    for info in infos:
        G.add_node(info["unit"], kind="unit", file=info["file"])
        for t in info["tipos"]:
            if t["local"] or t["name"] in tipos_def:
                continue
            tipos_def.add(t["name"])
            G.add_node(t["name"], kind="type", file=info["file"], line=t["line"],
                       record=t["record"], doc=t["doc"], source=t["source"],
                       requirements=";".join(t["reqs"]))
            G.add_edge(info["unit"], t["name"], kind="defines")
            for r in t["reqs"]:
                if r not in G:
                    G.add_node(r, kind="requirement")
                G.add_edge(t["name"], r, kind="traces")
        for v in info["variables"]:
            if v["name"] in vars_def or v["name"] in tipos_def:
                continue
            vars_def.add(v["name"])
            G.add_node(v["name"], kind="constant" if v["constant"] else "variable",
                       file=info["file"], line=v["line"], source=v["source"])
            G.add_edge(info["unit"], v["name"], kind="defines")
        for s in info["subprogramas"]:
            nid = s["name"]
            spec = spec_info.get(nid, {})
            doc = spec.get("doc") or s["doc"]           # preferimos la doc del .ads
            reqs = sorted(set(s["reqs"]) | set(spec.get("reqs", ())))
            # GraphML solo admite escalares: listas -> cadena separada por ';'
            G.add_node(nid, kind="subprogram", file=info["file"],
                       spec_file=spec.get("spec_file", ""),
                       start_line=s["start_line"], end_line=s["end_line"],
                       doc=doc, requirements=";".join(reqs))
            definidos.setdefault(nid, nid)
            G.add_edge(info["unit"], nid, kind="defines")
            for r in reqs:
                if r not in G:
                    G.add_node(r, kind="requirement")
                G.add_edge(nid, r, kind="traces")

    # Subprogramas declarados en spec sin cuerpo encontrado (p. ej. importados)
    for name, spec in spec_info.items():
        if name not in G:
            G.add_node(name, kind="subprogram", file=spec["spec_file"], spec_file=spec["spec_file"],
                       start_line=0, end_line=0, doc=spec["doc"],
                       requirements=";".join(sorted(spec["reqs"])))
            definidos.setdefault(name, name)
            for r in spec["reqs"]:
                if r not in G:
                    G.add_node(r, kind="requirement")
                G.add_edge(name, r, kind="traces")

    # Pasada 2: aristas. Cada `Nombre (...)` se clasifica según qué es Nombre:
    #   subprograma -> calls | tipo -> converts | variable -> uses | predefinido -> se ignora
    no_resueltas: dict[str, int] = {}
    conteo = {"calls": 0, "converts": 0, "uses": 0, "predefined": 0}
    for info in infos:
        for w in info["withs"]:
            if w not in G:
                G.add_node(w, kind="external")
            G.add_edge(info["unit"], w, kind="withs")
        for s in info["subprogramas"]:
            for call in s["calls"]:
                simple = call.rsplit(".", 1)[-1]  # pkg.proc -> proc
                if call in definidos or simple in definidos:
                    G.add_edge(s["name"], call if call in definidos else simple, kind="calls")
                    conteo["calls"] += 1
                elif simple in tipos_def:
                    G.add_edge(s["name"], simple, kind="converts")
                    conteo["converts"] += 1
                elif simple in vars_def:
                    G.add_edge(s["name"], simple, kind="uses")
                    conteo["uses"] += 1
                elif simple in PREDEFINED_TYPES:
                    conteo["predefined"] += 1
                else:
                    if call not in G:
                        G.add_node(call, kind="external")
                    no_resueltas[call] = no_resueltas.get(call, 0) + 1
                    G.add_edge(s["name"], call, kind="calls")

    subs = [(n, d) for n, d in G.nodes(data=True) if d.get("kind") == "subprogram"]
    stats = {
        "ficheros": len(ficheros),
        "nodos": G.number_of_nodes(),
        "aristas": G.number_of_edges(),
        "subprogramas": len(subs),
        "externos": sum(1 for _, d in G.nodes(data=True) if d.get("kind") == "external"),
        "requisitos": sum(1 for _, d in G.nodes(data=True) if d.get("kind") == "requirement"),
        "tipos": len(tipos_def),
        "variables": len(vars_def),
        "aristas_por_tipo": conteo,
        "subprogramas_sin_doc": sum(1 for _, d in subs if not d.get("doc")),
        "subprogramas_sin_requisito": sum(1 for _, d in subs if not d.get("requirements")),
        "llamadas_no_resueltas": sum(no_resueltas.values()),
        "top_no_resueltas": sorted(no_resueltas.items(), key=lambda kv: -kv[1])[:15],
        "top_llamados": sorted(
            ((n, sum(1 for _, _, d in G.in_edges(n, data=True) if d.get("kind") == "calls")) for n, _ in subs),
            key=lambda kv: -kv[1])[:15],
        "top_requisitos": sorted(
            ((n, G.in_degree(n)) for n, d in G.nodes(data=True) if d.get("kind") == "requirement"),
            key=lambda kv: -kv[1])[:15],
    }
    return G, stats


def exportar(G: nx.DiGraph, stats: dict, out: Path) -> None:
    nx.write_graphml(G, out.with_suffix(".graphml"))
    data = {
        "stats": {k: v for k, v in stats.items() if not k.startswith("top_")},
        "nodes": [{"id": n, **d} for n, d in G.nodes(data=True)],
        "edges": [{"source": u, "target": v, **d} for u, v, d in G.edges(data=True)],
    }
    out.with_suffix(".json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Grafo de código Ada con tree-sitter")
    ap.add_argument("src", type=Path, help="directorio raíz con fuentes .ads/.adb")
    ap.add_argument("--out", type=Path, default=Path("grafo_ada"), help="prefijo de salida")
    ap.add_argument("--req-pattern", default=REQ_PATTERN_DEFAULT,
                    help="regex de trazabilidad en comentarios (grupo 1 = lista de IDs)")
    ap.add_argument("--debug", action="store_true",
                    help="detener en el primer error con traceback completo")
    args = ap.parse_args()

    if not args.src.is_dir():
        sys.exit(f"{args.src} no es un directorio")
    patron_req = re.compile(args.req_pattern)

    G, stats = construir_grafo(args.src, patron_req, args.debug)
    exportar(G, stats, args.out)

    print(f"\n{stats['nodos']} nodos, {stats['aristas']} aristas "
          f"({stats['subprogramas']} subprogramas, {stats['tipos']} tipos, "
          f"{stats['variables']} variables, {stats['externos']} externos, "
          f"{stats['requisitos']} requisitos)")
    c = stats["aristas_por_tipo"]
    print(f"llamadas: {c['calls']}  |  conversiones de tipo: {c['converts']}  |  "
          f"accesos a variables globales: {c['uses']}  |  tipos predefinidos ignorados: {c['predefined']}")
    print(f"subprogramas sin comentario: {stats['subprogramas_sin_doc']}  |  "
          f"sin requisito: {stats['subprogramas_sin_requisito']}")
    print(f"llamadas no resueltas: {stats['llamadas_no_resueltas']}")
    print("\nMás llamados:")
    for n, k in stats["top_llamados"]:
        print(f"  {k:5d}  {n}")
    print("\nRequisitos con más subprogramas trazados:")
    for n, k in stats["top_requisitos"]:
        print(f"  {k:5d}  {n}")
    print("\nNo resueltas más frecuentes (librerías externas o nombres no capturados):")
    for n, k in stats["top_no_resueltas"]:
        print(f"  {k:5d}  {n}")
    print(f"\nGuardado en {args.out}.graphml y {args.out}.json")


if __name__ == "__main__":
    main()
