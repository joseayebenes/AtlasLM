#!/usr/bin/env python3
"""
pdf_struct.py — Convierte un PDF (o el markdown que genera markitdown) en
documentación markdown estructurada usando un LLM local (Gemma 4 a través de
una API compatible con OpenAI: Ollama, vLLM, llama.cpp server...).

Pipeline:
  1. Texto: markdown de entrada, o PDF -> markitdown.
  2. Secciones: corte por encabezados markdown (#) o, si no hay (markitdown no
     los marca en PDFs), por títulos numerados tipo "2.1 Track deletion".
     Las secciones grandes se trocean y las vacías se fusionan con la siguiente.
     Las páginas se siguen con los saltos de página (\\f) que deja markitdown.
  3. Map: el LLM extrae JSON estructurado de cada sección (caché en disco).
  4. Verificación: cada requisito extraído se busca en el texto original.
  5. Reduce: resumen global a partir de los resúmenes de sección.
  6. Escritura determinista de los markdowns: el LLM nunca escribe ficheros.

Estado: <salida>/estado.json registra cada etapa y las secciones con error o pendientes.
Se guarda tras cada sección, así que un error, un corte o Ctrl+C no pierde nada: al
volver a ejecutar, lo que ya funcionó sale de la caché y solo se reintenta lo fallido.

Uso:
  python pdf_struct.py documento.pdf --out salida --model gemma4
  python pdf_struct.py documento.md  --out salida --limit 3      # prueba rápida

Conexión con la API (URL, modelo, clave, proxy, certificados): en conexion.toml
(crea uno con `python conexion.py init`) o con las opciones --base-url, --model,
--proxy, --ca-cert... que tienen prioridad sobre el fichero. Ver conexion.py.

Requisitos:
  pip install openai
  pip install "markitdown[pdf]"     # solo si la entrada es un PDF
  pip install pymupdf               # recomendado: secciones a partir de los marcadores del PDF
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conexion as cx  # noqa: E402

PROMPT_VERSION = "v1"   # cámbialo si editas los prompts: invalida la caché

# --------------------------------------------------------------------------
# 0. Estado de la sesión (estado.json en la carpeta de salida de cada documento)
# --------------------------------------------------------------------------

class ErrorFatal(Exception):
    """Error tras el que no tiene sentido seguir (conexión, autenticación, límite de la API).
    El estado ya está guardado cuando se lanza: basta con volver a ejecutar."""


def ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha_fichero(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


class Estado:
    """Estado de procesamiento de un documento: etapas, secciones y lotes de embeddings
    fallidos o pendientes. Se escribe de forma atómica tras cada cambio, así que sobrevive
    a errores, cortes de red y Ctrl+C. Los resultados en sí están en las cachés: al
    relanzar solo se procesa lo que falta."""

    def __init__(self, out: Path):
        self.path = Path(out) / "estado.json"
        try:
            self.d = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.d = {}
        self.d.setdefault("etapas", {})
        self.d.setdefault("secciones", {})
        self.d.setdefault("lotes_embeddings", [])

    def guardar(self) -> None:
        self.d["actualizado"] = ahora()
        self.d["estado"] = self.estado_global()
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def etapa(self, nombre: str, estado: str, **extra) -> None:
        self.d["etapas"][nombre] = {"estado": estado, "fecha": ahora(), **extra}
        self.guardar()

    def seccion(self, sid: str, titulo: str, estado: str, error: str = "") -> None:
        previa = self.d["secciones"].get(sid, {})
        fallos = previa.get("fallos", 0) if previa.get("titulo") == titulo else 0
        e = {"titulo": titulo, "estado": estado, "fallos": fallos + (1 if estado == "error" else 0)}
        if error:
            e["error"] = error[:500]
        self.d["secciones"][sid] = e

    def pendientes(self) -> dict:
        return {"secciones": [{"id": k, **v} for k, v in sorted(self.d["secciones"].items())
                              if v["estado"] != "ok"],
                "lotes_embeddings": list(self.d.get("lotes_embeddings", [])),
                "etapas": [k for k, v in self.d["etapas"].items() if v["estado"] != "ok"]}

    def estado_global(self) -> str:
        p = self.pendientes()
        if any(v["estado"] == "interrumpido" for v in self.d["etapas"].values()):
            return "interrumpido"
        return "incompleto" if (p["secciones"] or p["lotes_embeddings"] or p["etapas"]) else "completo"


# --------------------------------------------------------------------------
# 1. Texto de entrada
# --------------------------------------------------------------------------

def leer_entrada(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            from markitdown import MarkItDown
        except ImportError:
            sys.exit('Para leer PDFs instala markitdown: pip install "markitdown[pdf]"')
        return MarkItDown().convert(str(path)).text_content
    return path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# 2. Secciones
# --------------------------------------------------------------------------

MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# "2", "2.1", "3.4.1", "A.1", "B.2.3" + título que empieza en mayúscula. Se descartan
# entradas de índice ("1 Intro ..... 4") y frases normativas.
NUM_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,5}|[A-Z](?:\.\d{1,3}){1,5})\.?\s{1,4}"
                         r"([A-ZÁÉÍÓÚÑ][^\n]{1,100})$")
ANEXO = re.compile(r"^((?:ANNEX|APPENDIX|ANEXO|APÉNDICE|Annex|Appendix|Anexo|Apéndice)\s+[A-Z0-9]{1,3})"
                   r"\b[\s.:–-]*(.{0,100})$")
NUM_SOLO = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,5}|[A-Z](?:\.\d{1,3}){1,5})\.?$")
TOC_LINE = re.compile(r"(\.{3,}|…)\s*\d+\s*$")
NORMATIVO = re.compile(r"\b(shall|must|should)\b", re.I)


def _motivo_rechazo(titulo: str, linea: str) -> str | None:
    if TOC_LINE.search(linea):
        return "entrada de índice"
    if titulo.endswith((".", ",", ";", ":")):
        return "termina en puntuación"
    if len(titulo.split()) > 12:
        return "más de 12 palabras"
    if NORMATIVO.search(titulo):
        return "frase normativa (shall/must)"
    return None


def es_titulo_numerado(linea: str, patron: re.Pattern) -> tuple[int, str] | None:
    l = linea.strip()
    m = ANEXO.match(l) if patron is NUM_HEADING else None
    if m and not TOC_LINE.search(l):
        return 1, f"{m.group(1)} {m.group(2)}".strip()
    m = patron.match(l)
    if not m:
        return None
    numero, titulo = m.group(1), m.group(2).strip()
    if _motivo_rechazo(titulo, linea):
        return None
    return numero.count(".") + 1, f"{numero} {titulo}"


def limpiar_cabeceras(texto: str) -> tuple[str, int]:
    """Quita cabeceras y pies de página repetidos (líneas cortas al principio o al final de
    muchas páginas, comparadas con los números sustituidos por #). Además de limpiar el
    texto, evita que un número de página se tome por un título."""
    paginas = texto.split("\f")
    if len(paginas) < 4:
        return texto, 0
    norm = lambda l: re.sub(r"\d+", "#", " ".join(l.split()).lower())
    cuenta: Counter = Counter()
    for pag in paginas:
        lineas = [l for l in pag.split("\n") if l.strip()]
        for l in set(norm(x) for x in lineas[:3] + lineas[-3:] if len(x.strip()) <= 120):
            cuenta[l] += 1
    umbral = max(3, int(len(paginas) * 0.3))
    repetidas = {l for l, n in cuenta.items() if n >= umbral}
    if not repetidas:
        return texto, 0
    quitadas, nuevas = 0, []
    for pag in paginas:
        lineas = pag.split("\n")
        idx = [i for i, l in enumerate(lineas) if l.strip()]
        bordes = set(idx[:3] + idx[-3:])
        conservar = []
        for i, l in enumerate(lineas):
            if i in bordes and len(l.strip()) <= 120 and norm(l) in repetidas:
                quitadas += 1
                continue
            conservar.append(l)
        nuevas.append("\n".join(conservar))
    return "\f".join(nuevas), quitadas


def _parece_titulo(l: str) -> bool:
    l = l.replace("\f", "").strip()
    return bool(l) and l[0].isupper() and not NUM_SOLO.match(l) and _motivo_rechazo(l, l) is None


def _unir_numeros_sueltos(lineas: list[str]) -> list[str]:
    """pdfminer suele separar el número del título ("3.2.1" en una línea y "Track
    Management" en la siguiente) e incluso agrupar varios números seguidos y después sus
    títulos ("1.1", "1.1.1", "Function group", "Track handling"). Se emparejan en orden."""
    out, i, n = [], 0, len(lineas)
    while i < n:
        if not NUM_SOLO.match(lineas[i].replace("\f", "").strip()):
            out.append(lineas[i])
            i += 1
            continue
        # tramo de números sueltos (con líneas en blanco entre medias)
        nums, j = [], i
        while j < n and (NUM_SOLO.match(lineas[j].replace("\f", "").strip()) or not lineas[j].strip()):
            if lineas[j].strip():
                nums.append(j)
            j += 1
        # tramo siguiente de títulos, tantos como números
        tits, k = [], j
        while k < n and len(tits) < len(nums) and (_parece_titulo(lineas[k]) or not lineas[k].strip()):
            if lineas[k].strip():
                tits.append(k)
            k += 1
        if not tits:
            out.extend(lineas[i:j])
            i = j
            continue
        pares = min(len(nums), len(tits))
        saltos = "\f" * sum(lineas[x].count("\f") for x in range(i, k))
        for a, b in zip(nums[:pares], tits[:pares]):
            num = lineas[a].replace("\f", "").strip().rstrip(".")
            out.append((saltos if a == nums[0] else "") + f"{num} {lineas[b].replace(chr(12), '').strip()}")
        out.extend(lineas[x] for x in nums[pares:])   # números sin título: se dejan
        i = tits[pares - 1] + 1
    return out


def detectar_secciones(texto: str, heading_regex: str | None, pagina_inicial: int = 1) -> list[dict]:
    """Secciones por títulos del texto (markdown # o numerados), con nivel, título, ruta,
    cuerpo y páginas."""
    patron_num = re.compile(heading_regex) if heading_regex else NUM_HEADING
    lineas = texto.split("\n")
    usa_md = sum(1 for l in lineas if MD_HEADING.match(l)) >= 2
    if not usa_md and not heading_regex:
        lineas = _unir_numeros_sueltos(lineas)

    secciones = []
    actual = {"level": 0, "title": "Preámbulo", "lines": [], "page_start": pagina_inicial,
              "page_end": pagina_inicial}
    pagina = pagina_inicial
    for linea in lineas:
        saltos = linea.count("\f")
        linea_limpia = linea.replace("\f", "")
        pagina += saltos
        cab = None
        if usa_md:
            m = MD_HEADING.match(linea_limpia)
            if m:
                cab = (len(m.group(1)), m.group(2).strip())
        else:
            cab = es_titulo_numerado(linea_limpia, patron_num)
        if cab:
            secciones.append(actual)
            actual = {"level": cab[0], "title": cab[1], "lines": [],
                      "page_start": pagina, "page_end": pagina}
        else:
            actual["lines"].append(linea_limpia)
            if linea_limpia.strip():
                actual["page_end"] = pagina
    secciones.append(actual)
    _rutas(secciones)
    for s in secciones:
        s["body"] = "\n".join(s.pop("lines")).strip()
    return [s for s in secciones if s["body"] or s["level"] > 0]


def _rutas(secciones: list[dict]) -> None:
    pila: list[tuple[int, str]] = []
    for s in secciones:
        while pila and pila[-1][0] >= s["level"] > 0:
            pila.pop()
        s["path"] = [t for _, t in pila] + [s["title"]]
        if s["level"] > 0:
            pila.append((s["level"], s["title"]))


def secciones_por_marcadores(pdf: Path, texto: str) -> tuple[list[dict], int, int] | None:
    """Secciones a partir de los marcadores (índice navegable) del propio PDF, que es la
    fuente de estructura más fiable. Devuelve (secciones, marcadores, localizados)."""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            return None
    try:
        with pymupdf.open(str(pdf)) as doc:
            toc = [t for t in doc.get_toc(simple=True) if t[2] >= 1 and str(t[1]).strip()]
    except Exception:
        return None
    if len(toc) < 3:
        return None

    inicios = [0] + [i + 1 for i, c in enumerate(texto) if c == "\f"]
    pagina_de = lambda pos: sum(1 for x in inicios if x <= pos)

    marcas, prev, localizados = [], 0, 0
    for nivel, titulo, pag in toc:
        titulo = " ".join(str(titulo).split())
        a = inicios[min(pag, len(inicios)) - 1]
        b = inicios[pag + 1] if pag + 1 < len(inicios) else len(texto)
        desde = max(a, prev)
        encontrado = None
        palabras = re.findall(r"\w+", titulo)
        for intento in (palabras, palabras[1:] if palabras and re.match(r"[\d.]+$", palabras[0]) else []):
            if not intento or desde >= b:
                continue
            m = re.compile(r"\b" + r"\W+".join(map(re.escape, intento)), re.I).search(texto, desde, b)
            if m:
                encontrado = m
                break
        if encontrado:
            ini_linea = texto.rfind("\n", 0, encontrado.start()) + 1
            fin_linea = texto.find("\n", encontrado.end())
            pos, cuerpo_desde = max(ini_linea, prev), (fin_linea if fin_linea >= 0 else len(texto))
            localizados += 1
        else:
            pos = cuerpo_desde = desde
        marcas.append((pos, cuerpo_desde, int(nivel), titulo))
        prev = max(prev, cuerpo_desde)

    secciones = [{"level": 0, "title": "Preámbulo", "page_start": 1, "page_end": pagina_de(marcas[0][0]),
                  "body": texto[:marcas[0][0]]}]
    for k, (pos, cuerpo_desde, nivel, titulo) in enumerate(marcas):
        fin = marcas[k + 1][0] if k + 1 < len(marcas) else len(texto)
        cuerpo_desde = min(cuerpo_desde, fin)
        secciones.append({"level": nivel, "title": titulo, "page_start": pagina_de(pos),
                          "page_end": pagina_de(max(pos, fin - 1)), "body": texto[cuerpo_desde:fin]})
    _rutas(secciones)
    for s in secciones:
        s["body"] = s["body"].replace("\f", "\n").strip()
    return [s for s in secciones if s["body"] or s["level"] > 0], len(toc), localizados


def diagnostico_titulos(texto: str, ejemplos: int = 3) -> dict[str, list[str]]:
    """Líneas que empiezan como un título numerado, agrupadas por qué se aceptan o no."""
    grupos: dict[str, list[str]] = {}
    lineas = _unir_numeros_sueltos(texto.split("\n"))
    patron = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,5}|[A-Z](?:\.\d{1,3}){1,5})\.?\s+(\S.*)$")
    for l in lineas:
        l = l.replace("\f", "").strip()
        m = patron.match(l)
        if not m:
            continue
        titulo = m.group(2).strip()
        if es_titulo_numerado(l, NUM_HEADING):
            motivo = "aceptado como título"
        elif not titulo[0].isupper():
            motivo = "empieza en minúscula o número"
        else:
            motivo = _motivo_rechazo(titulo, l) or "otro formato"
        grupos.setdefault(motivo, []).append(l[:90])
    return grupos


def trocear(texto: str, max_chars: int) -> list[str]:
    """Parte un texto largo por párrafos (y por líneas si un párrafo es enorme)."""
    if len(texto) <= max_chars:
        return [texto]
    partes, buf = [], ""
    for par in re.split(r"\n\s*\n", texto):
        trozos = [par] if len(par) <= max_chars else \
            [par[i:i + max_chars] for i in range(0, len(par), max_chars)]
        for t in trozos:
            if buf and len(buf) + len(t) + 2 > max_chars:
                partes.append(buf)
                buf = ""
            buf = f"{buf}\n\n{t}" if buf else t
    if buf:
        partes.append(buf)
    return partes


ESTRUCTURAS = ("auto", "marcadores", "titulos", "tamano")


def preparar_secciones(texto: str, max_chars: int, min_chars: int,
                       heading_regex: str | None, pdf: Path | None = None,
                       estructura: str = "auto", info: dict | None = None) -> list[dict]:
    """Secciones listas para el modelo. Estructura: 'marcadores' del PDF si los tiene,
    si no 'titulos' numerados del texto, y si no bloques de 'tamano' fijo. Las secciones
    demasiado grandes se subdividen por los títulos que contengan antes de trocearlas."""
    info = info if info is not None else {}
    brutas, fuente = None, ""
    # Reunir "número" + "título" separados por pdfminer antes de buscar la estructura
    if not heading_regex and sum(1 for l in texto.split("\n") if MD_HEADING.match(l)) < 2:
        texto = "\n".join(_unir_numeros_sueltos(texto.split("\n")))
    if estructura in ("auto", "marcadores") and pdf is not None and pdf.suffix.lower() == ".pdf":
        r = secciones_por_marcadores(pdf, texto)
        if r and r[2] >= max(3, r[1] * 0.5):
            brutas = r[0]
            fuente = f"marcadores del PDF ({r[2]} de {r[1]} localizados en el texto)"
        elif estructura == "marcadores":
            fuente = ("el PDF no tiene marcadores utilizables" if not r else
                      f"solo {r[2]} de {r[1]} marcadores localizados") + "; se usan títulos"
    if brutas is None and estructura != "tamano":
        brutas = detectar_secciones(texto, heading_regex)
        n_tit = sum(1 for s in brutas if s["level"] > 0)
        fuente = (fuente + "; " if fuente else "") + f"títulos del texto ({n_tit} detectados)"
        if n_tit < 2:
            brutas = None
    if brutas is None:
        fuente = (fuente + "; " if fuente else "") + "bloques de tamaño fijo"
        brutas = [{"level": 1, "title": f"Bloque {i + 1}", "path": [f"Bloque {i + 1}"],
                   "body": t, "page_start": 0, "page_end": 0}
                  for i, t in enumerate(trocear(texto.replace("\f", "\n"), max_chars))]
    info["fuente"] = fuente

    # Subdividir por títulos internos las secciones que no caben (p. ej. un marcador de
    # capítulo con muchas subsecciones sin marcador propio)
    expandidas, subdivididas = [], 0
    for s in brutas:
        if len(s["body"]) > max_chars and estructura != "tamano":
            subs = detectar_secciones(s["body"], heading_regex, s["page_start"] or 1)
            if sum(1 for x in subs if x["level"] > 0) >= 2:
                subdivididas += 1
                for x in subs:
                    if x["level"] == 0:
                        x.update(level=s["level"], title=s["title"], path=list(s["path"]))
                    else:
                        x.update(level=s["level"] + x["level"], path=s["path"] + x["path"])
                    x["page_end"] = max(x["page_end"], x["page_start"])
                expandidas += subs
                continue
        expandidas.append(s)
    info["subdivididas"] = subdivididas

    # Las cabeceras sin texto propio ("2 Requirements") se descartan: su título
    # ya va en la ruta de sus hijas. Las secciones cortas se agrupan con la
    # anterior, solo dentro del mismo capítulo y sin pasar de max_chars.
    fusionadas: list[dict] = []
    for s in expandidas:
        if not s["body"]:
            continue
        prev = fusionadas[-1] if fusionadas else None
        if (prev and prev["path"][0] == s["path"][0]
                and (len(s["body"]) < min_chars or len(prev["body"]) < min_chars)
                and len(prev["body"]) + len(s["body"]) + len(s["title"]) + 3 <= max_chars):
            prev["body"] += f"\n\n{s['title']}\n{s['body']}"
            prev["includes"].append(s["title"])
            prev["page_end"] = max(prev["page_end"], s["page_end"])
            continue
        fusionadas.append(dict(s, includes=[]))

    # Trocea lo que aún sea demasiado grande
    finales, gigantes = [], []
    for s in fusionadas:
        partes = trocear(s["body"], max_chars)
        if len(partes) > 5:
            gigantes.append((s["title"], len(partes)))
        for k, parte in enumerate(partes, 1):
            titulo = s["title"] if len(partes) == 1 else f"{s['title']} (parte {k}/{len(partes)})"
            finales.append(dict(s, title=titulo, body=parte))
    for i, s in enumerate(finales, 1):
        s["id"] = f"{i:03d}"
    info["gigantes"] = gigantes
    return finales


# --------------------------------------------------------------------------
# 3. LLM
# --------------------------------------------------------------------------

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "requirements": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["id", "text"]}},
        "definitions": {"type": "array", "items": {
            "type": "object",
            "properties": {"term": {"type": "string"}, "definition": {"type": "string"}},
            "required": ["term", "definition"]}},
        "entities": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "kind": {"type": "string"},
                           "description": {"type": "string"}},
            "required": ["name", "kind", "description"]}},
        "references": {"type": "array", "items": {"type": "string"}},
        "open_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "key_points", "requirements", "definitions",
                 "entities", "references", "open_issues"],
}

SECTION_SYSTEM = """You are a meticulous technical analyst. You receive ONE section of a \
larger document that was converted from PDF to plain text: line breaks may fall mid-sentence \
and tables may be flattened into one cell per line. Extract structured information from THIS \
section only and answer with a single JSON object.

Rules:
- Use only information present in the section. Never invent identifiers, values or requirements.
- summary: 2-4 sentences on what the section specifies or explains.
- key_points: the most important facts, values, constraints or behaviours (max 8).
- requirements: every normative statement (shall / must / will / should, or anything \
explicitly labelled as a requirement). Copy `text` VERBATIM from the section, joining broken \
lines. Put the identifier in `id` only if one appears in the text; otherwise "".
- definitions: terms, acronyms and abbreviations explicitly defined in the section.
- entities: the most relevant named systems, subsystems, functions, interfaces, messages, \
signals, parameters, equipment or documents (max 15). `kind` is one of: system, function, \
interface, message, parameter, equipment, document, other. Keep descriptions under 15 words.
- references: references to other sections, documents or standards (max 10).
- open_issues: TBD, TBC, TBA or explicitly open points, with what they refer to.
- Empty lists are fine. Never repeat an item. Output compact JSON (no indentation) and nothing \
else. {lang_rule}"""

SUMMARY_SYSTEM = """You receive the section-by-section summaries of a technical document. \
Write an overview of the whole document as a JSON object with: "summary" (one paragraph, \
5-8 sentences), "purpose" (one sentence), "scope" (one sentence) and "main_topics" \
(list of 3-10 short topics). Use only the information given. {lang_rule}"""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "purpose": {"type": "string"},
                   "scope": {"type": "string"},
                   "main_topics": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "purpose", "scope", "main_topics"],
}


def regla_idioma(lang: str) -> str:
    if not lang:
        return "Write in the same language as the document."
    return (f"Write summary, key_points, descriptions and overviews in {lang}; keep "
            f"requirement text, terms and definitions verbatim in the original language.")


def _es_respuesta(obj, claves: list[str] | None) -> bool:
    """Evita tomar por respuesta un objeto interior (p. ej. un requisito suelto) de un JSON
    roto: debe tener al menos una de las claves esperadas."""
    return isinstance(obj, dict) and (not claves or any(k in obj for k in claves))


def parse_json(texto: str, claves: list[str] | None = None) -> dict:
    """El objeto JSON más grande de la respuesta con alguna de las claves esperadas, aunque
    venga rodeado de texto, en un bloque ```json``` o precedido de razonamiento con llaves."""
    t = re.sub(r"```(?:json)?", "", texto or "")
    dec = json.JSONDecoder()
    mejor, mejor_len, i = None, -1, 0
    while (i := t.find("{", i)) >= 0:
        try:
            obj, fin = dec.raw_decode(t, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if _es_respuesta(obj, claves) and fin - i > mejor_len:
            mejor, mejor_len = obj, fin - i
            i = fin
        else:
            i += 1
    if mejor is None:
        raise ValueError("la respuesta no contiene un objeto JSON válido")
    return mejor


def reparar_json(texto: str, claves: list[str] | None = None) -> dict | None:
    """Recupera lo aprovechable de un JSON cortado (salida truncada por longitud) o con
    comas sobrantes: corta en el último elemento completo y cierra lo que quede abierto."""
    t = re.sub(r"```(?:json)?", "", texto or "")
    ini = t.find("{")
    if ini < 0:
        return None
    t = t[ini:]
    pila: list[str] = []
    cortes: list[tuple[int, list[str]]] = []
    en_cadena = escape = False
    for i, c in enumerate(t):
        if en_cadena:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                en_cadena = False
            continue
        if c == '"':
            en_cadena = True
        elif c in "{[":
            pila.append(c)
        elif c in "}]":
            if pila:
                pila.pop()
            if not pila:
                cortes.append((i + 1, []))
                break
            cortes.append((i + 1, list(pila)))
        elif c == ",":
            cortes.append((i, list(pila)))
    for pos, abiertos in reversed(cortes[-200:]):
        cand = t[:pos].rstrip().rstrip(",") + "".join("}" if x == "{" else "]" for x in reversed(abiertos))
        cand = re.sub(r",\s*([}\]])", r"\1", cand)
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if _es_respuesta(obj, claves):
            return obj
    return None


class RespuestaTruncada(Exception):
    """La salida del modelo se cortó por longitud. `parcial` es lo recuperable, o None."""

    def __init__(self, msg: str, parcial: dict | None):
        super().__init__(msg)
        self.parcial = parcial


class LLM:
    def __init__(self, cfg: cx.ServiceConfig):
        from openai import BadRequestError
        self.cfg = cfg
        self.client, self.proxy = cx.crear_cliente(cfg)
        self.BadRequestError = BadRequestError
        self.model = cfg.model
        self.temperature = cfg.temperature
        self.schema_ok = True   # se desactiva si el servidor no admite json_schema
        self._lock = threading.Lock()

    def _llamar(self, messages: list[dict], schema: dict, nombre: str) -> tuple[str, str]:
        while True:
            kwargs = dict(model=self.model, temperature=self.temperature, messages=messages)
            if self.cfg.max_tokens:
                kwargs["max_tokens"] = self.cfg.max_tokens
            if self.schema_ok:
                kwargs["response_format"] = {"type": "json_schema", "json_schema":
                                             {"name": nombre, "schema": schema}}
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except self.BadRequestError as e:
                if "response_format" in kwargs:
                    with self._lock:
                        if self.schema_ok:
                            print(f"[aviso] el servidor no acepta json_schema, sigo sin él: {e}",
                                  file=sys.stderr)
                            self.schema_ok = False
                    continue
                raise
            ch = resp.choices[0]
            return ch.message.content or "", (ch.finish_reason or "")

    def json(self, system: str, user: str, schema: dict, nombre: str,
             guardar_fallo=None) -> dict:
        """Pide un objeto JSON. Si la respuesta no es válida: la repara si es un detalle,
        lanza RespuestaTruncada si se cortó por longitud (para dividir la entrada), y si
        no, pide una corrección al modelo mostrándole su error (en vez de repetir igual)."""
        claves = schema.get("required") or list(schema.get("properties", {}))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        texto, fin = self._llamar(messages, schema, nombre)
        for intento in (1, 2):
            if fin != "length":                 # una respuesta cortada nunca se da por buena
                try:
                    return parse_json(texto, claves)
                except ValueError as e:
                    error = e
            else:
                error = ValueError("respuesta cortada por longitud")
            if guardar_fallo:
                guardar_fallo(intento, texto, fin)
            reparado = reparar_json(texto, claves)
            if fin == "length":
                raise RespuestaTruncada(f"respuesta cortada por longitud ({len(texto)} car.)", reparado)
            if reparado is not None:
                return reparado
            if intento == 2:
                break
            messages = messages + [
                {"role": "assistant", "content": texto[:3000]},
                {"role": "user", "content": f"That was not a valid JSON object ({error}). Reply "
                                            f"again with ONLY the JSON object, nothing before or after it."}]
            texto, fin = self._llamar(messages, schema, nombre)
        raise RuntimeError(f"el modelo no devolvió JSON válido ni tras pedir corrección: {error}")


def normalizar(d: dict) -> dict:
    """Garantiza claves y tipos aunque el modelo se salte el esquema."""
    def lista(x):
        return x if isinstance(x, list) else []

    def objs(x, claves):
        out = []
        for o in lista(x):
            if isinstance(o, dict):
                out.append({k: str(o.get(k, "") or "").strip() for k in claves})
        return out

    return {
        "summary": str(d.get("summary", "") or "").strip(),
        "key_points": [str(x).strip() for x in lista(d.get("key_points")) if str(x).strip()],
        "requirements": [r for r in objs(d.get("requirements"), ["id", "text"]) if r["text"]],
        "definitions": [x for x in objs(d.get("definitions"), ["term", "definition"]) if x["term"]],
        "entities": [x for x in objs(d.get("entities"), ["name", "kind", "description"]) if x["name"]],
        "references": [str(x).strip() for x in lista(d.get("references")) if str(x).strip()],
        "open_issues": [str(x).strip() for x in lista(d.get("open_issues")) if str(x).strip()],
    }


def _prompt_seccion(s: dict, doc_title: str) -> str:
    paginas = f"{s['page_start']}-{s['page_end']}" if s["page_start"] else "?"
    return (f"Document: {doc_title}\nSection path: {' > '.join(s['path'])}\n"
            f"Pages: {paginas}\n\n<section>\n{s['body']}\n</section>")


def fichero_cache_seccion(cache: Path | None, model: str, s: dict, doc_title: str,
                          lang: str) -> Path | None:
    if not cache:
        return None
    clave = hashlib.sha256(f"{PROMPT_VERSION}|{model}|{lang}|{_prompt_seccion(s, doc_title)}"
                           .encode("utf-8")).hexdigest()[:20]
    return cache / f"{clave}.json"


def partir_en_dos(texto: str) -> list[str]:
    """Divide por el párrafo (o línea, o frase) más cercano a la mitad."""
    for sep in (r"\n\s*\n", r"\n", r"(?<=[.;:])\s+"):
        trozos = [x for x in re.split(sep, texto) if x.strip()]
        if len(trozos) < 2:
            continue
        mitad, acum = len(texto) / 2, 0
        for k, tr in enumerate(trozos[:-1], 1):
            acum += len(tr)
            if acum >= mitad:
                break
        unir = "\n\n" if sep.startswith(r"\n\s") else ("\n" if sep == r"\n" else " ")
        return [unir.join(trozos[:k]), unir.join(trozos[k:])]
    return [texto]


def fusionar_analisis(partes: list[dict]) -> dict:
    out = normalizar({})
    out["summary"] = " ".join(p["summary"] for p in partes if p["summary"])
    out["key_points"] = [k for p in partes for k in p["key_points"]][:10]
    for campo, clave in (("requirements", "text"), ("definitions", "term"), ("entities", "name")):
        vistos = set()
        for p in partes:
            for x in p[campo]:
                if x[clave].lower() not in vistos:
                    vistos.add(x[clave].lower())
                    out[campo].append(x)
    for campo in ("references", "open_issues"):
        out[campo] = list(dict.fromkeys(x for p in partes for x in p[campo]))
    return out


MAX_DIVISIONES = 3   # una sección se puede partir hasta en 2^3 = 8 trozos


def _analizar_texto(llm: LLM, s: dict, cuerpo: str, doc_title: str, lang: str,
                    fallos: Path | None, nivel: int = 0, sufijo: str = "") -> dict:
    sub = dict(s, body=cuerpo, path=s["path"] + ([f"(parte {sufijo.lstrip('.')})"] if sufijo else []))
    system = SECTION_SYSTEM.replace("{lang_rule}", regla_idioma(lang))

    def guardar(intento: int, texto: str, fin: str) -> None:
        if fallos:
            fallos.mkdir(parents=True, exist_ok=True)
            (fallos / f"{s['id']}{sufijo}_intento{intento}.txt").write_text(
                f"# sección {s['id']}{sufijo} {s['title']} | finish_reason={fin or '?'} | "
                f"{len(texto)} car. | {ahora()}\n\n{texto}", encoding="utf-8")

    try:
        return normalizar(llm.json(system, _prompt_seccion(sub, doc_title), SECTION_SCHEMA,
                                   "section", guardar))
    except (RespuestaTruncada, RuntimeError) as e:
        mitades = partir_en_dos(cuerpo) if nivel < MAX_DIVISIONES and len(cuerpo) > 1500 else [cuerpo]
        if len(mitades) < 2:
            if isinstance(e, RespuestaTruncada) and e.parcial is not None:
                log(f"  {s['id']}{sufijo}: respuesta cortada y no se puede dividir más; "
                    f"se guarda lo recuperado")
                return normalizar(e.parcial)
            raise
        motivo = "respuesta cortada por longitud" if isinstance(e, RespuestaTruncada) else "JSON no válido"
        log(f"  {s['id']}{sufijo}: {motivo}; se divide en 2 partes ({len(cuerpo)} car.)")
        return fusionar_analisis([
            _analizar_texto(llm, s, m, doc_title, lang, fallos, nivel + 1, f"{sufijo}.{k}")
            for k, m in enumerate(mitades, 1)])


def analizar_seccion(llm: LLM, s: dict, doc_title: str, lang: str, cache: Path | None,
                     fallos: Path | None = None) -> dict:
    fichero = fichero_cache_seccion(cache, llm.model, s, doc_title, lang)
    if fichero and fichero.exists():
        return json.loads(fichero.read_text(encoding="utf-8"))
    t0 = time.monotonic()
    try:
        res = _analizar_texto(llm, s, s["body"], doc_title, lang, fallos)
    finally:
        s["_dur"] = time.monotonic() - t0
    if fichero:
        fichero.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def resumen_global(llm: LLM, secciones: list[dict], doc_title: str, lang: str,
                   max_chars: int, cache: Path | None = None) -> dict:
    """Reduce jerárquico: si los resúmenes no caben en una llamada, se agrupan."""
    items = [f"[{s['title']}] {s['analysis']['summary']}" for s in secciones
             if s["analysis"]["summary"]]
    clave = hashlib.sha256(f"{PROMPT_VERSION}|{llm.model}|{lang}|{doc_title}|{items}"
                           .encode("utf-8")).hexdigest()[:20]
    fichero = cache / f"overview_{clave}.json" if cache else None
    if fichero and fichero.exists():
        return json.loads(fichero.read_text(encoding="utf-8"))
    res = _resumen_global(llm, items, doc_title, lang, max_chars)
    if fichero:
        fichero.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def _resumen_global(llm: LLM, items: list[str], doc_title: str, lang: str, max_chars: int) -> dict:
    system = SUMMARY_SYSTEM.replace("{lang_rule}", regla_idioma(lang))
    while len("\n".join(items)) > max_chars and len(items) > 1:
        grupos, buf = [], []
        for it in items:
            if buf and len("\n".join(buf + [it])) > max_chars:
                grupos.append(buf)
                buf = []
            buf.append(it)
        grupos.append(buf)
        items = [llm.json(system, f"Document: {doc_title}\n\n" + "\n".join(g),
                          SUMMARY_SCHEMA, "overview").get("summary", "") for g in grupos]
    if not items:
        return {"summary": "", "purpose": "", "scope": "", "main_topics": []}
    d = llm.json(system, f"Document: {doc_title}\n\n" + "\n".join(items), SUMMARY_SCHEMA, "overview")
    return {"summary": str(d.get("summary", "")), "purpose": str(d.get("purpose", "")),
            "scope": str(d.get("scope", "")),
            "main_topics": [str(x) for x in d.get("main_topics", []) if x]}


# --------------------------------------------------------------------------
# 4. Verificación y consolidación
# --------------------------------------------------------------------------

def _canon(s: str) -> str:
    return re.sub(r"\W+", " ", s.replace("-\n", "")).lower().strip()


def verificar(req_text: str, fuente: str) -> str:
    """literal | parcial | no_encontrado — detecta requisitos inventados o reescritos."""
    a, b = _canon(req_text), _canon(fuente)
    if a and a in b:
        return "literal"
    palabras = set(a.split())
    if palabras and len(palabras & set(b.split())) / len(palabras) >= 0.85:
        return "parcial"
    return "no_encontrado"


def consolidar(secciones: list[dict]) -> dict:
    glosario: dict[str, dict] = {}
    entidades: dict[str, dict] = {}
    requisitos, pendientes, vistos = [], [], set()
    for s in secciones:
        a = s["analysis"]
        for r in a["requirements"]:
            clave = r["id"].upper() or _canon(r["text"])
            if clave in vistos:
                continue
            vistos.add(clave)
            r["status"] = verificar(r["text"], s["body"])
            requisitos.append({**r, "section": s["id"], "section_title": s["title"]})
        for d in a["definitions"]:
            g = glosario.setdefault(d["term"].lower(), {"term": d["term"], "definitions": [],
                                                       "sections": []})
            if d["definition"] and d["definition"] not in g["definitions"]:
                g["definitions"].append(d["definition"])
            if s["id"] not in g["sections"]:
                g["sections"].append(s["id"])
        for e in a["entities"]:
            x = entidades.setdefault(e["name"].lower(), {"name": e["name"], "kinds": Counter(),
                                                        "description": "", "sections": []})
            x["kinds"][e["kind"].lower() or "other"] += 1
            if not x["description"] and e["description"]:
                x["description"] = e["description"]
            if s["id"] not in x["sections"]:
                x["sections"].append(s["id"])
        for p in a["open_issues"]:
            pendientes.append({"text": p, "section": s["id"], "section_title": s["title"]})
    for x in entidades.values():
        x["kind"] = x.pop("kinds").most_common(1)[0][0]
    return {"glossary": sorted(glosario.values(), key=lambda g: g["term"].lower()),
            "entities": sorted(entidades.values(), key=lambda e: (e["kind"], e["name"].lower())),
            "requirements": requisitos, "open_issues": pendientes}


# --------------------------------------------------------------------------
# 5. Escritura de markdowns
# --------------------------------------------------------------------------

def slug(t: str) -> str:
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = re.sub(r"[^\w\s-]", "", t.lower())
    return re.sub(r"[\s_-]+", "-", t).strip("-")[:50] or "seccion"


def celda(t: str) -> str:
    return " ".join(str(t).split()).replace("|", "\\|")


def frontmatter(campos: dict) -> str:
    # JSON es YAML válido: evita problemas de escapado con comillas y dos puntos
    return "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}"
                               for k, v in campos.items()) + "\n---\n"


def ruta_seccion(s: dict) -> str:
    return f"secciones/{s['id']}-{slug(s['title'])}.md"


def paginas(s: dict) -> str:
    if not s["page_start"]:
        return ""
    return str(s["page_start"]) if s["page_start"] == s["page_end"] else \
        f"{s['page_start']}-{s['page_end']}"


def escribir_seccion(out: Path, s: dict, fuente: str, con_texto: bool, reqs: list[dict]) -> None:
    a = s["analysis"]
    mis_reqs = [r for r in reqs if r["section"] == s["id"]]
    md = [frontmatter({"id": s["id"], "title": s["title"], "path": s["path"],
                       "pages": paginas(s), "source": fuente, "includes": s["includes"],
                       "analysis_status": "error" if s.get("analysis_error") else "ok",
                       "requirements": [r["id"] for r in mis_reqs if r["id"]]}),
          f"# {s['title']}\n",
          f"> {' › '.join(s['path'])}" + (f" · páginas {paginas(s)}" if paginas(s) else "") +
          (f"  \n> Incluye también: {'; '.join(s['includes'])}" if s["includes"] else "") + "\n"]
    if s.get("analysis_error"):
        md.append(f"> ⚠ Análisis pendiente (se reintentará en la próxima ejecución): "
                  f"{s['analysis_error'][:200]}\n")
    if a["summary"]:
        md.append(f"## Resumen\n\n{a['summary']}\n")
    if a["key_points"]:
        md.append("## Puntos clave\n\n" + "\n".join(f"- {p}" for p in a["key_points"]) + "\n")
    if mis_reqs:
        md.append("## Requisitos\n\n| ID | Texto | Verificación |\n|---|---|---|\n" +
                  "\n".join(f"| {celda(r['id']) or '—'} | {celda(r['text'])} | {r['status']} |"
                            for r in mis_reqs) + "\n")
    if a["definitions"]:
        md.append("## Definiciones\n\n" + "\n".join(
            f"- **{d['term']}**: {d['definition']}" for d in a["definitions"]) + "\n")
    if a["entities"]:
        md.append("## Entidades\n\n" + "\n".join(
            f"- **{e['name']}** ({e['kind']})" + (f": {e['description']}" if e["description"] else "")
            for e in a["entities"]) + "\n")
    if a["references"]:
        md.append("## Referencias\n\n" + "\n".join(f"- {r}" for r in a["references"]) + "\n")
    if a["open_issues"]:
        md.append("## Pendientes\n\n" + "\n".join(f"- {p}" for p in a["open_issues"]) + "\n")
    if con_texto:
        md.append("## Texto original\n\n~~~~text\n" + s["body"] + "\n~~~~\n")
    (out / ruta_seccion(s)).write_text("\n".join(md), encoding="utf-8")


def escribir_todo(out: Path, doc_title: str, fuente: str, model: str, secciones: list[dict],
                  overview: dict, cons: dict, con_texto: bool) -> dict:
    (out / "secciones").mkdir(parents=True, exist_ok=True)
    for s in secciones:
        escribir_seccion(out, s, fuente, con_texto, cons["requirements"])

    reqs = cons["requirements"]
    estado = Counter(r["status"] for r in reqs)
    by_id = {s["id"]: s for s in secciones}
    link = lambda sid: f"[{sid}]({ruta_seccion(by_id[sid])})"
    extra = lambda s: f" + {len(s['includes'])} subsecc." if s["includes"] else ""

    # index.md
    md = [frontmatter({"title": doc_title, "source": fuente, "model": model,
                       "generated": datetime.now().isoformat(timespec="seconds"),
                       "sections": len(secciones), "requirements": len(reqs)}),
          f"# {doc_title}\n"]
    if overview["summary"]:
        md.append(f"## Resumen\n\n{overview['summary']}\n")
    if overview["purpose"] or overview["scope"]:
        md.append(f"**Propósito:** {overview['purpose']}\n\n**Alcance:** {overview['scope']}\n")
    if overview["main_topics"]:
        md.append("## Temas principales\n\n" + "\n".join(f"- {t}" for t in overview["main_topics"]) + "\n")
    md.append("## Documentos\n\n- [Requisitos](requisitos.md) — "
              f"{len(reqs)} ({estado['literal']} literales, {estado['parcial']} parciales, "
              f"{estado['no_encontrado']} no encontrados en el texto)\n"
              f"- [Glosario](glosario.md) — {len(cons['glossary'])} términos\n"
              f"- [Entidades](entidades.md) — {len(cons['entities'])}\n"
              f"- [Pendientes](pendientes.md) — {len(cons['open_issues'])}\n")
    md.append("## Índice de secciones\n\n| # | Sección | Págs. | Req. | Resumen |\n|---|---|---|---|---|\n" +
              "\n".join(
                  f"| {s['id']} | [{celda(s['title'])}]({ruta_seccion(s)}){extra(s)} | {paginas(s)} | "
                  f"{sum(1 for r in reqs if r['section'] == s['id'])} | "
                  f"{'⚠ análisis pendiente' if s.get('analysis_error') else celda(s['analysis']['summary'].split('. ')[0])[:160]} |"
                  for s in secciones) + "\n")
    (out / "index.md").write_text("\n".join(md), encoding="utf-8")

    # requisitos.md
    md = [frontmatter({"title": f"Requisitos — {doc_title}", "count": len(reqs),
                       "verification": dict(estado)}),
          "# Requisitos\n",
          "La columna Verificación indica si el texto extraído aparece literalmente en el "
          "documento. Revisa a mano los `no_encontrado`.\n",
          "| ID | Texto | Sección | Verificación |\n|---|---|---|---|"]
    md += [f"| {celda(r['id']) or '—'} | {celda(r['text'])} | {link(r['section'])} | {r['status']} |"
           for r in reqs]
    (out / "requisitos.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # glosario.md
    md = [frontmatter({"title": f"Glosario — {doc_title}", "count": len(cons["glossary"])}),
          "# Glosario\n", "| Término | Definición | Secciones |\n|---|---|---|"]
    md += [f"| **{celda(g['term'])}** | {celda(' / '.join(g['definitions'][:3]))} | "
           f"{', '.join(link(x) for x in g['sections'])} |" for g in cons["glossary"]]
    (out / "glosario.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # entidades.md (agrupadas por tipo)
    md = [frontmatter({"title": f"Entidades — {doc_title}", "count": len(cons["entities"])}),
          "# Entidades\n"]
    tipo_actual = None
    for e in cons["entities"]:
        if e["kind"] != tipo_actual:
            tipo_actual = e["kind"]
            md.append(f"\n## {tipo_actual}\n")
        md.append(f"- **{e['name']}**" + (f": {e['description']}" if e["description"] else "") +
                  f" — {', '.join(link(x) for x in e['sections'])}")
    (out / "entidades.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # pendientes.md
    md = [frontmatter({"title": f"Pendientes — {doc_title}", "count": len(cons["open_issues"])}),
          "# Pendientes (TBD / TBC)\n"]
    md += [f"- {p['text']} — {link(p['section'])}" for p in cons["open_issues"]] or ["Ninguno."]
    (out / "pendientes.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # estructura.json: todo junto para herramientas (MCP, búsquedas...)
    datos = {"title": doc_title, "source": fuente, "model": model, "overview": overview,
             "sections": [{k: s[k] for k in ("id", "title", "path", "includes", "page_start",
                                              "page_end", "analysis", "body")} | {"file": ruta_seccion(s)}
                          for s in secciones],
             **cons}
    (out / "estructura.json").write_text(json.dumps(datos, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    return datos


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def titulo_documento(texto: str, path: Path) -> str:
    for linea in texto.split("\n")[:40]:
        m = MD_HEADING.match(linea)
        if m and len(m.group(1)) == 1:
            return m.group(2).strip()
    for linea in texto.replace("\f", "\n").split("\n")[:15]:
        if 10 < len(linea.strip()) < 120:
            return linea.strip()
    return path.stem


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def procesar(input_path: Path, out: Path | None = None, *, llm_cfg: cx.ServiceConfig,
             lang: str = "", title: str | None = None, max_chars: int = 12000,
             min_chars: int = 300, heading_regex: str | None = None, workers: int = 1,
             limit: int = 0, con_texto: bool = True, usar_cache: bool = True,
             estructura: str = "auto") -> dict:
    """Pipeline completo con estado persistente. Devuelve la estructura (estructura.json).
    Las secciones que fallan se registran en estado.json y se reintentan al relanzar;
    las que ya funcionaron salen de la caché sin llamar al modelo."""
    out = out or Path(f"{input_path.stem}_estructurado")
    out.mkdir(parents=True, exist_ok=True)
    estado = Estado(out)

    huella = sha_fichero(input_path)
    previa = estado.d.get("entrada", {})
    if previa.get("hash") and previa["hash"] != huella:
        log("el documento ha cambiado desde la última ejecución: se reinicia su estado")
        estado.d = {"etapas": {}, "secciones": {}, "lotes_embeddings": []}
    estado.d["entrada"] = {"ruta": str(input_path.resolve()), "hash": huella,
                           "bytes": input_path.stat().st_size}
    estado.d["modelo"] = llm_cfg.model
    estado.d["opciones"] = {"lang": lang, "max_chars": max_chars, "min_chars": min_chars,
                            "heading_regex": heading_regex, "estructura": estructura}

    # Extracción
    try:
        texto = leer_entrada(input_path)
    except Exception as e:
        estado.etapa("extraccion", "error", error=f"{type(e).__name__}: {e}"[:500])
        raise
    texto, quitadas = limpiar_cabeceras(texto)
    (out / "_texto_extraido.md").write_text(texto, encoding="utf-8")
    estado.etapa("extraccion", "ok", caracteres=len(texto), cabeceras_quitadas=quitadas)
    doc_title = title or titulo_documento(texto, input_path)

    info: dict = {}
    secciones = preparar_secciones(texto, max_chars, min_chars, heading_regex, input_path,
                                   estructura, info)
    if limit:
        secciones = secciones[:limit]
    log(f"'{doc_title}': {len(texto)} caracteres, {len(secciones)} secciones")
    log(f"estructura: {info['fuente']}" +
        (f"; {info['subdivididas']} secciones grandes subdivididas por sus títulos" if info["subdivididas"] else "") +
        (f"; {quitadas} líneas de cabecera/pie quitadas" if quitadas else ""))
    for titulo, n in info["gigantes"][:5]:
        log(f"  ⚠ '{titulo[:60]}' se trocea en {n} partes: no se detectan títulos dentro "
            f"(prueba --dry-run para ver por qué)")

    llm = LLM(llm_cfg)
    log(f"LLM: {llm_cfg.model} en {llm_cfg.base_url}" +
        (f" vía proxy {cx.ocultar_credenciales(llm.proxy)}" if llm.proxy else ""))
    cache = out / ".cache" if usar_cache else None
    if cache:
        cache.mkdir(exist_ok=True)

    # Análisis por sección: el estado se guarda tras cada una
    vivas = {s["id"] for s in secciones}
    estado.d["secciones"] = {k: v for k, v in estado.d["secciones"].items() if k in vivas}
    for s in secciones:
        estado.seccion(s["id"], s["title"], "pendiente")
    estado.etapa("analisis", "en_curso", total=len(secciones))

    en_cache = {s["id"] for s in secciones
                if (f := fichero_cache_seccion(cache, llm.model, s, doc_title, lang)) and f.exists()}
    log(f"análisis: {len(en_cache)} de {len(secciones)} secciones ya en caché; "
        f"{len(secciones) - len(en_cache)} por procesar")

    t0 = time.time()
    errores = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            fallos = out / "_fallos_llm"
            futuros = {pool.submit(analizar_seccion, llm, s, doc_title, lang, cache, fallos): s
                       for s in secciones}
            for n, fut in enumerate(as_completed(futuros), 1):
                s = futuros[fut]
                try:
                    s["analysis"] = fut.result()
                    estado.seccion(s["id"], s["title"], "ok")
                    resultado = f"{len(s['analysis']['requirements'])} req." + \
                        (" (caché)" if s["id"] in en_cache else "")
                except Exception as e:
                    if cx.es_error_conexion(e):
                        pool.shutdown(wait=False, cancel_futures=True)
                        msg = cx.diagnosticar(e)
                        estado.seccion(s["id"], s["title"], "error", msg)
                        estado.etapa("analisis", "interrumpido", error=msg)
                        raise ErrorFatal(f"error de conexión con el LLM: {msg}") from e
                    errores += 1
                    s["analysis"] = normalizar({})
                    s["analysis_error"] = f"{type(e).__name__}: {e}"
                    estado.seccion(s["id"], s["title"], "error", s["analysis_error"])
                    resultado = f"ERROR: {e}"
                estado.guardar()
                dur = f"{s['_dur']:.0f}s, " if "_dur" in s else ""
                log(f"  [{n}/{len(secciones)}] {s['id']} {s['title'][:50]} — {resultado} "
                    f"({dur}total {time.time() - t0:.0f}s)")
    except KeyboardInterrupt:
        estado.etapa("analisis", "interrumpido", error="cancelado por el usuario")
        raise
    estado.etapa("analisis", "ok" if not errores else "incompleto",
                 total=len(secciones), errores=errores)

    # Resumen global
    log("resumen global...")
    try:
        overview = resumen_global(llm, secciones, doc_title, lang, max_chars, cache)
        estado.etapa("resumen", "ok")
    except Exception as e:
        if cx.es_error_conexion(e):
            estado.etapa("resumen", "interrumpido", error=cx.diagnosticar(e))
            raise ErrorFatal(f"error de conexión con el LLM: {cx.diagnosticar(e)}") from e
        estado.etapa("resumen", "error", error=f"{type(e).__name__}: {e}"[:500])
        overview = {"summary": "", "purpose": "", "scope": "", "main_topics": []}

    cons = consolidar(secciones)
    datos = escribir_todo(out, doc_title, input_path.name, llm_cfg.model, secciones, overview,
                          cons, con_texto)
    datos["out_dir"] = str(out.resolve())
    estado.etapa("escritura", "ok")

    reqs = Counter(r["status"] for r in cons["requirements"])
    log(f"\nListo en {time.time() - t0:.0f}s -> {out}/index.md\n"
        f"  secciones: {len(secciones)} ({errores} con error)\n"
        f"  requisitos: {len(cons['requirements'])} "
        f"(literal {reqs['literal']}, parcial {reqs['parcial']}, "
        f"no encontrado {reqs['no_encontrado']})\n"
        f"  glosario: {len(cons['glossary'])}  entidades: {len(cons['entities'])}  "
        f"pendientes: {len(cons['open_issues'])}")
    if errores or estado.d["etapas"].get("resumen", {}).get("estado") != "ok":
        log(f"  ⚠ análisis incompleto: vuelve a ejecutar para reintentar solo lo fallido "
            f"(detalle en {estado.path}; respuestas no válidas del modelo en {out / '_fallos_llm'})")
    return datos


def main() -> None:
    ap = argparse.ArgumentParser(description="PDF/markdown -> markdowns estructurados con un LLM")
    ap.add_argument("input", type=Path, help="fichero .pdf o .md")
    ap.add_argument("--out", type=Path, default=None, help="directorio de salida")
    ap.add_argument("--lang", default="", help="idioma de resúmenes (p. ej. 'Spanish'); vacío = el del documento")
    ap.add_argument("--title", default=None, help="título del documento (por defecto, se detecta)")
    ap.add_argument("--max-chars", type=int, default=12000, help="tamaño máximo de sección enviada al modelo")
    ap.add_argument("--min-chars", type=int, default=300, help="secciones más cortas se agrupan con la anterior")
    ap.add_argument("--heading-regex", default=None, help="regex propia para títulos (grupo1=número, grupo2=título)")
    ap.add_argument("--estructura", choices=ESTRUCTURAS, default="auto",
                    help="de dónde sacar las secciones: marcadores del PDF, títulos del texto, o "
                         "bloques de tamaño fijo (auto: marcadores si los hay, si no títulos)")
    ap.add_argument("--workers", type=int, default=1, help="peticiones en paralelo al servidor")
    ap.add_argument("--limit", type=int, default=0, help="procesar solo las N primeras secciones")
    ap.add_argument("--dry-run", action="store_true", help="solo mostrar las secciones detectadas, sin LLM")
    ap.add_argument("--no-source", action="store_true", help="no incluir el texto original en cada sección")
    ap.add_argument("--no-cache", action="store_true")
    cx.añadir_args(ap, embeddings=False)
    args = ap.parse_args()

    if not args.input.exists():
        sys.exit(f"no existe {args.input}")

    if args.dry_run:
        texto, quitadas = limpiar_cabeceras(leer_entrada(args.input))
        info: dict = {}
        secciones = preparar_secciones(texto, args.max_chars, args.min_chars, args.heading_regex,
                                       args.input, args.estructura, info)
        if args.limit:
            secciones = secciones[:args.limit]
        log(f"'{args.title or titulo_documento(texto, args.input)}': {len(texto)} caracteres, "
            f"{len(secciones)} secciones")
        for s in secciones:
            print(f"  {s['id']}  p.{paginas(s) or '?':>7}  {len(s['body']):6d} car.  "
                  f"{' > '.join(s['path'])}" + (f"  + {s['includes']}" if s["includes"] else ""))
        tam = sorted(len(s["body"]) for s in secciones)
        print(f"\nestructura: {info['fuente']}")
        if info["subdivididas"]:
            print(f"  {info['subdivididas']} secciones grandes subdivididas por sus títulos internos")
        if quitadas:
            print(f"  {quitadas} líneas de cabecera/pie de página quitadas")
        print(f"{len(secciones)} secciones; tamaño mediano {tam[len(tam) // 2]} car., "
              f"máximo {tam[-1]}; {sum(t < 1000 for t in tam)} con menos de 1000 car.")
        for titulo, n in info["gigantes"][:10]:
            print(f"  ⚠ '{titulo[:60]}' se trocea en {n} partes por tamaño")
        print("peticiones de análisis según --min-chars (con el --max-chars actual):")
        for mc in (300, 1000, 2000, 4000, 8000):
            n = len(preparar_secciones(texto, args.max_chars, mc, args.heading_regex,
                                       args.input, args.estructura))
            print(f"  --min-chars {mc:5d}: {n} secciones" + ("   <- actual" if mc == args.min_chars else ""))
        if info["gigantes"] or "marcadores" not in info["fuente"]:
            grupos = diagnostico_titulos(texto)
            if grupos:
                print("\nlíneas que empiezan como un título numerado:")
                for motivo, ls in sorted(grupos.items(), key=lambda kv: -len(kv[1])):
                    print(f"  {len(ls):5d}  {motivo}")
                    for ejemplo in ls[:3]:
                        print(f"           {ejemplo}")
                print("  Si tus títulos tienen otro formato, usa --heading-regex con grupo 1 = "
                      "número y grupo 2 = título.")
        return

    try:
        procesar(args.input, args.out, llm_cfg=cx.cargar(args).llm, lang=args.lang,
                 title=args.title, max_chars=args.max_chars, min_chars=args.min_chars,
                 heading_regex=args.heading_regex, workers=args.workers, limit=args.limit,
                 con_texto=not args.no_source, usar_cache=not args.no_cache,
                 estructura=args.estructura)
    except ErrorFatal as e:
        sys.exit(f"\n{e}\nEl estado está guardado: vuelve a ejecutar el mismo comando para continuar.")
    except KeyboardInterrupt:
        sys.exit("\nInterrumpido. El estado está guardado: vuelve a ejecutar para continuar.")


if __name__ == "__main__":
    main()
