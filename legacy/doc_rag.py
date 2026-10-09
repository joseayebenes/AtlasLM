#!/usr/bin/env python3
"""
doc_rag.py — RAG local para documentación técnica, sobre pdf_struct.py.

Indexa cada documento a varias granularidades (resumen global, resúmenes de
sección, fragmentos de texto, requisitos, definiciones, pendientes) en un único
fichero SQLite con vectores (numpy) y búsqueda léxica BM25 (FTS5). La consulta
combina ambas (búsqueda híbrida con Reciprocal Rank Fusion), amplía el contexto
con los fragmentos vecinos y responde citando las fuentes [n].

Comandos:
  python doc_rag.py ingest spec.pdf otro.pdf docs/   # uno o varios ficheros o carpetas
  python doc_rag.py status                     # estado de la sesión y lotes pendientes
  python doc_rag.py retry                      # reprocesar solo lo que quedó pendiente
  python doc_rag.py index spec_estructurado/   # indexar una salida previa de pdf_struct
  python doc_rag.py search "delete track latency" -k 8
  python doc_rag.py ask "¿Cuánto tarda en borrarse una traza?"
  python doc_rag.py docs                       # documentos indexados
  python doc_rag.py mcp                        # servidor MCP (stdio) para un agente
  python doc_rag.py check                      # probar conexión, proxy y certificados

Conexión (URL, modelos, claves, proxy, certificados): en conexion.toml, que se crea
con `python conexion.py init`. Las opciones --base-url, --model, --embed-model,
--proxy, --ca-cert... tienen prioridad sobre el fichero y van ANTES del comando:
  python doc_rag.py --config otra.toml ask "..."

Estado de la sesión: cada documento guarda estado.json en su carpeta de salida (etapas,
secciones con error, lotes de embeddings fallidos) y la sesión completa se resume en
<db>_sesion.json. Los resultados buenos quedan en caché (análisis por sección y vectores
por texto), así que al relanzar solo se llama a la API para lo que falló o faltaba.

Requisitos:  pip install openai numpy   (+ "markitdown[pdf]" para PDFs, "mcp" para el modo mcp)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conexion as cx  # noqa: E402
import pdf_struct as ps  # noqa: E402

# La base de datos (y con ella la sesión y las salidas) no depende del directorio desde
# el que se lance: por defecto va junto a los scripts. Se cambia con --db o RAG_DB.
DEFAULT_DB = os.environ.get("RAG_DB") or str(Path(__file__).resolve().parent / "rag.db")

CHUNK_CHARS = 1200      # tamaño objetivo de fragmento para recuperación
CHUNK_OVERLAP = 200     # solape entre fragmentos consecutivos
CONTEXT_CHARS = 14000   # máximo de contexto que se pasa al LLM al responder

# Prefijos que algunos modelos de embeddings esperan para consulta y documento
EMBED_PREFIXES = {
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
    "nomic-embed": ("search_query: ", "search_document: "),
    "e5": ("query: ", "passage: "),
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------

class Embedder:
    def __init__(self, cfg: cx.ServiceConfig):
        self.client, self.proxy = cx.crear_cliente(cfg)
        self.model = cfg.model
        self.batch = cfg.batch
        self.q_prefix, self.d_prefix = next(
            (v for k, v in EMBED_PREFIXES.items() if k in self.model.lower()), ("", ""))

    def _embed(self, textos: list[str]) -> np.ndarray:
        vecs = []
        for i in range(0, len(textos), self.batch):
            resp = self.client.embeddings.create(model=self.model, input=textos[i:i + self.batch])
            vecs.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        m = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(norms == 0, 1, norms)

    def clave(self, texto: str) -> str:
        """Clave de caché de un vector: depende del modelo, del prefijo y del texto."""
        return hashlib.sha256(f"{self.model}\x00{self.d_prefix}{texto}".encode("utf-8")).hexdigest()[:32]

    def documents(self, textos: list[str]) -> np.ndarray:
        return self._embed([self.d_prefix + t for t in textos])

    def query(self, texto: str) -> np.ndarray:
        return self._embed([self.q_prefix + texto])[0]

    def queries(self, textos: list[str]) -> np.ndarray:
        """Varias consultas en una sola petición (importa con límites de pet./minuto)."""
        return self._embed([self.q_prefix + t for t in textos])


# --------------------------------------------------------------------------
# Almacén: SQLite (registros + vectores) + FTS5 (BM25)
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE, title TEXT, source TEXT,
    out_dir TEXT, overview TEXT, ingested_at TEXT);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY, doc_id INTEGER, kind TEXT, section_id TEXT,
    section_title TEXT, path TEXT, page_start INTEGER, page_end INTEGER,
    ref TEXT, seq INTEGER, text TEXT, embed_text TEXT, vector BLOB);
CREATE INDEX IF NOT EXISTS idx_records_doc ON records(doc_id);
CREATE INDEX IF NOT EXISTS idx_records_ref ON records(ref);
CREATE TABLE IF NOT EXISTS embed_cache (clave TEXT PRIMARY KEY, vector BLOB);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5("
                            "embed_text, ref, tokenize='unicode61 remove_diacritics 2')")
            self.fts = True
        except sqlite3.OperationalError:
            log("[aviso] este sqlite no tiene FTS5: solo búsqueda vectorial")
            self.fts = False
        self._matrix = None
        self._ids = None

    def meta(self, key: str, value: str | None = None) -> str | None:
        if value is not None:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            self.db.commit()
            return value
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def comprobar_embed_model(self, model: str) -> None:
        actual = self.meta("embed_model")
        if actual and actual != model:
            sys.exit(f"La base de datos se creó con '{actual}' y ahora usas '{model}'. "
                     f"Usa el mismo modelo o una base de datos nueva (--db).")
        self.meta("embed_model", model)

    def cache_get(self, claves: list[str]) -> dict[str, np.ndarray]:
        out = {}
        for i in range(0, len(claves), 500):
            parte = claves[i:i + 500]
            for clave, blob in self.db.execute(
                    f"SELECT clave, vector FROM embed_cache WHERE clave IN ({','.join('?' * len(parte))})",
                    parte):
                out[clave] = np.frombuffer(blob, dtype=np.float32)
        return out

    def cache_put(self, items: dict[str, np.ndarray]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO embed_cache VALUES (?, ?)",
                            [(k, v.astype(np.float32).tobytes()) for k, v in items.items()])
        self.db.commit()

    def tiene_documento(self, name: str) -> bool:
        return self.db.execute("SELECT 1 FROM documents WHERE name = ?", (name,)).fetchone() is not None

    def borrar_documento(self, name: str) -> None:
        row = self.db.execute("SELECT id FROM documents WHERE name = ?", (name,)).fetchone()
        if not row:
            return
        if self.fts:
            self.db.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM records WHERE doc_id = ?)",
                            (row[0],))
        self.db.execute("DELETE FROM records WHERE doc_id = ?", (row[0],))
        self.db.execute("DELETE FROM documents WHERE id = ?", (row[0],))

    def guardar(self, doc: dict, registros: list[dict], vectores: np.ndarray) -> int:
        self.borrar_documento(doc["name"])
        cur = self.db.execute(
            "INSERT INTO documents (name, title, source, out_dir, overview, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc["name"], doc["title"], doc["source"], doc["out_dir"],
             json.dumps(doc["overview"], ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))
        doc_id = cur.lastrowid
        for r, v in zip(registros, vectores):
            cur = self.db.execute(
                "INSERT INTO records (doc_id, kind, section_id, section_title, path, page_start, "
                "page_end, ref, seq, text, embed_text, vector) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, r["kind"], r.get("section_id", ""), r.get("section_title", ""),
                 r.get("path", ""), r.get("page_start", 0), r.get("page_end", 0),
                 r.get("ref", ""), r.get("seq", 0), r["text"], r["embed_text"],
                 v.astype(np.float32).tobytes()))
            if self.fts:
                self.db.execute("INSERT INTO fts (rowid, embed_text, ref) VALUES (?, ?, ?)",
                                (cur.lastrowid, r["embed_text"], r.get("ref", "")))
        self.db.commit()
        self._matrix = None
        return doc_id

    # --- búsqueda ---------------------------------------------------------

    def _cargar_matriz(self):
        if self._matrix is None:
            rows = self.db.execute("SELECT id, vector FROM records ORDER BY id").fetchall()
            self._ids = np.array([r["id"] for r in rows], dtype=np.int64)
            self._matrix = (np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
                            if rows else np.zeros((0, 1), dtype=np.float32))
        return self._ids, self._matrix

    def filtro(self, document: str, kinds: list[str] | None) -> set[int] | None:
        if not document and not kinds:
            return None
        sql, params = "SELECT r.id FROM records r JOIN documents d ON d.id = r.doc_id WHERE 1=1", []
        if document:
            sql += " AND (d.name = ? OR d.title LIKE ?)"
            params += [document, f"%{document}%"]
        if kinds:
            sql += f" AND r.kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        return {r[0] for r in self.db.execute(sql, params)}

    def denso(self, qvec: np.ndarray, n: int, permitidos: set[int] | None) -> list[int]:
        ids, m = self._cargar_matriz()
        if len(ids) == 0:
            return []
        if m.shape[1] != qvec.shape[0]:
            sys.exit("Dimensión de embeddings distinta a la del índice: ¿otro modelo de embeddings?")
        scores = m @ qvec
        orden = np.argsort(-scores)
        out = []
        for i in orden:
            rid = int(ids[i])
            if permitidos is None or rid in permitidos:
                out.append(rid)
                if len(out) >= n:
                    break
        return out

    def lexico(self, consulta: str, n: int, permitidos: set[int] | None) -> list[int]:
        if not self.fts:
            return []
        tokens = re.findall(r"[\w][\w\-./]*", consulta)
        if not tokens:
            return []
        # cada token entre comillas: los IDs con guiones se buscan como frase exacta
        q = " OR ".join('"' + t.replace('"', '') + '"' for t in tokens)
        try:
            rows = self.db.execute("SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) "
                                   "LIMIT ?", (q, n * 4)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r[0] for r in rows if permitidos is None or r[0] in permitidos][:n]

    def por_ref(self, consulta: str, permitidos: set[int] | None) -> list[int]:
        """Coincidencia exacta de IDs (requisitos, términos) mencionados en la consulta."""
        candidatos = {t.upper() for t in re.findall(r"[A-Za-z0-9][\w\-.]*[\-_.\d][\w\-.]*", consulta)}
        if not candidatos:
            return []
        rows = self.db.execute(
            f"SELECT id FROM records WHERE UPPER(ref) IN ({','.join('?' * len(candidatos))})",
            list(candidatos)).fetchall()
        return [r[0] for r in rows if permitidos is None or r[0] in permitidos]

    def registros(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        rows = self.db.execute(
            "SELECT r.*, d.name AS doc_name, d.title AS doc_title, d.out_dir FROM records r "
            f"JOIN documents d ON d.id = r.doc_id WHERE r.id IN ({','.join('?' * len(ids))})",
            ids).fetchall()
        por_id = {r["id"]: {k: r[k] for k in r.keys() if k != "vector"} for r in rows}
        return [por_id[i] for i in ids if i in por_id]

    def vecinos(self, rec: dict, radio: int = 1) -> list[dict]:
        """Fragmentos adyacentes de la misma sección (contexto 'small-to-big')."""
        if rec["kind"] != "chunk":
            return [rec]
        rows = self.db.execute(
            "SELECT id FROM records WHERE doc_id = ? AND kind = 'chunk' AND section_id = ? "
            "AND seq BETWEEN ? AND ? ORDER BY seq",
            (rec["doc_id"], rec["section_id"], rec["seq"] - radio, rec["seq"] + radio)).fetchall()
        return self.registros([r[0] for r in rows])


# --------------------------------------------------------------------------
# Construcción de registros a partir de la estructura de pdf_struct
# --------------------------------------------------------------------------

def reflow(texto: str) -> str:
    """Une las líneas cortadas por el PDF dentro de cada párrafo."""
    pars = [" ".join(l.strip() for l in p.split("\n") if l.strip())
            for p in re.split(r"\n\s*\n", texto)]
    return "\n\n".join(p for p in pars if p)


def fragmentar(texto: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Fragmentos de ~size caracteres por párrafos y frases, con solape."""
    unidades = []
    for par in reflow(texto).split("\n\n"):
        if len(par) <= size:
            unidades.append(par)
        else:
            unidades.extend(re.split(r"(?<=[.;:!?])\s+", par))
    chunks, buf = [], ""
    for u in unidades:
        while len(u) > size:                       # frase gigantesca (tabla aplanada)
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(u[:size])
            u = u[size - overlap:]
        if buf and len(buf) + len(u) + 1 > size:
            chunks.append(buf)
            cola = buf[-overlap:]
            m = re.search(r"[.;:!?]\s+", cola)          # el solape empieza en frase completa
            buf = cola[m.end():] if m else (cola[cola.find(" ") + 1:] if " " in cola else "")
        buf = f"{buf} {u}".strip() if buf else u
    if buf:
        chunks.append(buf)
    return chunks


def construir_registros(datos: dict) -> list[dict]:
    titulo = datos["title"]
    regs = []
    ov = datos.get("overview") or {}
    if ov.get("summary"):
        texto = "\n".join(x for x in [ov.get("summary", ""), f"Purpose: {ov.get('purpose', '')}",
                                      f"Scope: {ov.get('scope', '')}",
                                      "Topics: " + ", ".join(ov.get("main_topics", []))] if x)
        regs.append({"kind": "overview", "text": texto,
                     "embed_text": f"{titulo} — document overview\n{texto}"})

    for s in datos["sections"]:
        a = s.get("analysis") or {}
        ruta = " > ".join(s.get("path") or [s["title"]])
        base = {"section_id": s["id"], "section_title": s["title"], "path": ruta,
                "page_start": s.get("page_start") or 0, "page_end": s.get("page_end") or 0}
        cabecera = f"{titulo} > {ruta}"

        if a.get("summary"):
            texto = a["summary"] + ("\n- " + "\n- ".join(a["key_points"]) if a.get("key_points") else "")
            regs.append({**base, "kind": "section_summary", "text": texto,
                         "embed_text": f"{cabecera}\n{texto}"})

        # Contextual retrieval: cada fragmento se embebe con su ruta y el resumen de su sección
        contexto = f"{cabecera}\n{a.get('summary', '')}".strip()
        for k, ch in enumerate(fragmentar(s.get("body", ""))):
            regs.append({**base, "kind": "chunk", "seq": k, "text": ch,
                         "embed_text": f"{contexto}\n\n{ch}"})

    for r in datos.get("requirements", []):
        sec = next((s for s in datos["sections"] if s["id"] == r["section"]), {})
        texto = (f"{r['id']}: " if r["id"] else "") + r["text"]
        regs.append({"kind": "requirement", "ref": r["id"], "text": texto,
                     "section_id": r["section"], "section_title": r.get("section_title", ""),
                     "path": " > ".join(sec.get("path", [])),
                     "page_start": sec.get("page_start") or 0, "page_end": sec.get("page_end") or 0,
                     "embed_text": f"{titulo} > requirement\n{texto}"})
    titulos = {s["id"]: s["title"] for s in datos["sections"]}
    for g in datos.get("glossary", []):
        texto = f"{g['term']}: {' / '.join(g['definitions'][:3])}"
        sid = (g.get("sections") or [""])[0]
        regs.append({"kind": "definition", "ref": g["term"], "text": texto,
                     "section_id": sid, "section_title": titulos.get(sid, ""),
                     "embed_text": f"{titulo} > glossary\n{texto}"})
    for p in datos.get("open_issues", []):
        regs.append({"kind": "open_issue", "text": p["text"], "section_id": p["section"],
                     "section_title": p.get("section_title", ""),
                     "embed_text": f"{titulo} > open issue (TBD)\n{p['text']}"})
    return regs


def indexar(store: Store, emb: Embedder, datos: dict, nombre: str | None = None,
            estado: ps.Estado | None = None) -> bool:
    """Embebe por lotes con caché y guarda el documento en el índice. Los lotes que fallan
    se registran en el estado y el resto se indexa igualmente. Devuelve True si no queda
    ningún lote pendiente."""
    if not datos["sections"] or "body" not in datos["sections"][0]:
        sys.exit("estructura.json sin texto de sección: vuelve a ejecutar pdf_struct.py "
                 "(con la caché es inmediato) para regenerarlo.")
    store.comprobar_embed_model(emb.model)
    regs = construir_registros(datos)
    claves = [emb.clave(r["embed_text"]) for r in regs]
    vectores = store.cache_get(claves)
    faltan = [i for i, k in enumerate(claves) if k not in vectores]
    lotes = [faltan[i:i + emb.batch] for i in range(0, len(faltan), emb.batch)]
    log(f"embeddings: {len(regs)} registros, {len(regs) - len(faltan)} en caché, "
        f"{len(faltan)} por calcular en {len(lotes)} lotes ({emb.model})")

    def describir_lote(n: int, lote: list[int], error: str) -> dict:
        return {"lote": n, "registros": len(lote), "error": error[:500], "fecha": ps.ahora(),
                "tipos": sorted({regs[i]["kind"] for i in lote}),
                "secciones": sorted({regs[i].get("section_id", "") for i in lote} - {""}),
                "claves": [claves[i] for i in lote]}

    fallidos = []
    if estado:
        estado.etapa("embeddings", "en_curso", total=len(regs), lotes=len(lotes))
    t0 = time.time()
    try:
        for n, lote in enumerate(lotes, 1):
            try:
                vecs = emb.documents([regs[i]["embed_text"] for i in lote])
                nuevos = {claves[i]: v for i, v in zip(lote, vecs)}
                store.cache_put(nuevos)              # persistido lote a lote
                vectores.update(nuevos)
                log(f"  lote {n}/{len(lotes)} ok ({time.time() - t0:.0f}s)")
            except Exception as e:
                if cx.es_error_conexion(e):
                    msg = cx.diagnosticar(e)
                    fallidos += [describir_lote(m, l, f"no procesado: {msg}")
                                 for m, l in enumerate(lotes[n - 1:], n)]
                    if estado:
                        estado.d["lotes_embeddings"] = fallidos
                        estado.etapa("embeddings", "interrumpido", error=msg)
                    raise ps.ErrorFatal(f"error de conexión con los embeddings: {msg}") from e
                fallidos.append(describir_lote(n, lote, f"{type(e).__name__}: {e}"))
                log(f"  lote {n}/{len(lotes)} ERROR: {e}")
                if estado:
                    estado.d["lotes_embeddings"] = fallidos
                    estado.guardar()
    except KeyboardInterrupt:
        if estado:
            estado.d["lotes_embeddings"] = fallidos + [
                describir_lote(m, l, "no procesado: cancelado por el usuario")
                for m, l in enumerate(lotes, 1) if not all(claves[i] in vectores for i in l)
                and m not in {f["lote"] for f in fallidos}]
            estado.etapa("embeddings", "interrumpido", error="cancelado por el usuario")
        raise

    if estado:
        estado.d["lotes_embeddings"] = fallidos
        estado.etapa("embeddings", "ok" if not fallidos else "incompleto",
                     total=len(regs), sin_vector=sum(f["registros"] for f in fallidos))

    # Se indexa lo que tiene vector; lo pendiente entra al relanzar
    ok = [i for i, k in enumerate(claves) if k in vectores]
    doc = {"name": nombre or Path(datos["source"]).stem, "title": datos["title"],
           "source": datos["source"], "out_dir": datos.get("out_dir", ""),
           "overview": datos.get("overview", {})}
    if ok:
        store.guardar(doc, [regs[i] for i in ok], np.vstack([vectores[claves[i]] for i in ok]))
    tipos = Counter(regs[i]["kind"] for i in ok)
    if estado:
        estado.etapa("indice", "ok" if not fallidos else "incompleto",
                     documento=doc["name"], registros=len(ok))
    log(f"indexado '{doc['name']}': {dict(tipos)}" +
        (f"  ⚠ {len(regs) - len(ok)} registros pendientes en {len(fallidos)} lotes" if fallidos else ""))
    return not fallidos


# --------------------------------------------------------------------------
# Sesión de ingesta (varios ficheros)
# --------------------------------------------------------------------------

EXTENSIONES = {".pdf", ".md", ".markdown", ".txt"}


class Sesion:
    """<db>_sesion.json: un registro por fichero con su estado, opciones y pendientes."""

    def __init__(self, db: str):
        db = Path(db).expanduser().resolve()
        self.path = db.with_name(db.stem + "_sesion.json")
        try:
            self.d = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.d = {"archivos": {}}

    def guardar(self) -> None:
        self.d["actualizado"] = ps.ahora()
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def entrada(self, ruta: Path) -> dict:
        return self.d["archivos"].setdefault(str(ruta.resolve()), {})

    def conocida(self, ruta: Path) -> dict | None:
        return self.d["archivos"].get(str(ruta.resolve()))

    def nombres_en_uso(self) -> dict[str, str]:
        return {e["nombre"]: r for r, e in self.d["archivos"].items() if e.get("nombre")}

    def actualizar(self, ruta: Path, **campos) -> None:
        if campos.get("error"):
            campos["error"] = " ".join(str(campos["error"]).split())[:500]
        self.entrada(ruta).update(campos, fecha=ps.ahora())
        self.guardar()


def expandir(rutas: list[Path]) -> list[Path]:
    out = []
    for r in rutas:
        if r.is_dir():
            out += sorted(p for p in r.rglob("*") if p.suffix.lower() in EXTENSIONES and p.is_file())
        elif r.exists():
            out.append(r)
        else:
            log(f"[aviso] no existe {r}")
    vistos, unicos = set(), []
    for p in out:
        if p.resolve() not in vistos:
            vistos.add(p.resolve())
            unicos.append(p)
    return unicos


def nombres_unicos(archivos: list[Path], sesion: "Sesion") -> dict[Path, str]:
    """Un fichero ya conocido conserva su nombre; uno nuevo usa su nombre de fichero, o
    carpeta_nombre si choca con otro."""
    en_uso = sesion.nombres_en_uso()            # nombre -> ruta
    cuenta = Counter(p.stem for p in archivos)
    out = {}
    for p in archivos:
        previa = sesion.conocida(p)
        if previa and previa.get("nombre"):
            out[p] = previa["nombre"]
            continue
        nombre = p.stem if cuenta[p.stem] == 1 else f"{p.parent.name}_{p.stem}"
        base, k = nombre, 2
        while (en_uso.get(nombre) not in (None, str(p.resolve()))) or nombre in out.values():
            nombre, k = f"{base}_{k}", k + 1
        out[p] = nombre
    return out


def al_dia(e: dict, ruta: Path, conn: cx.Conexion, store: Store) -> bool:
    return (e.get("estado") == "completo" and e.get("hash") == ps.sha_fichero(ruta)
            and e.get("llm_model") == conn.llm.model and e.get("embed_model") == conn.embeddings.model
            and store.tiene_documento(e.get("nombre", "")))


def ingest_archivos(archivos: list[Path], opciones: dict[Path, dict], conn: cx.Conexion,
                    store: Store, emb: Embedder, sesion: Sesion, force: bool = False) -> int:
    """Procesa varios ficheros. Un error propio de un fichero no detiene a los demás;
    un error fatal (conexión, límite, credenciales) guarda el estado y para la sesión."""
    for f in archivos:                         # registrar todos antes de empezar
        e = sesion.entrada(f)
        if not (e.get("estado") == "completo" and not force):
            e.update(estado="pendiente", **{k: str(v) if isinstance(v, Path) else v
                                            for k, v in opciones[f].items()})
    sesion.guardar()

    resumen = Counter()
    for n, f in enumerate(archivos, 1):
        e = sesion.entrada(f)
        op = opciones[f]
        log(f"\n=== [{n}/{len(archivos)}] {f}")
        if not force and al_dia(e, f, conn, store):
            log("al día (sin cambios desde la última ingesta completa): se omite")
            resumen["omitido"] += 1
            continue
        sesion.actualizar(f, estado="en_curso", error="", **{k: str(v) if isinstance(v, Path) else v
                                                            for k, v in op.items()})
        try:
            datos = ps.procesar(f, Path(op["out_dir"]), llm_cfg=conn.llm, lang=op["lang"],
                                workers=op["workers"], max_chars=op["max_chars"],
                                min_chars=op.get("min_chars", 300),
                                heading_regex=op["heading_regex"],
                                estructura=op.get("estructura", "auto"))
            estado = ps.Estado(Path(op["out_dir"]))
            indexar(store, emb, datos, op["nombre"], estado)
            pend = estado.pendientes()
            final = estado.estado_global()
            sesion.actualizar(f, estado=final, hash=ps.sha_fichero(f), llm_model=conn.llm.model,
                              embed_model=conn.embeddings.model,
                              secciones_fallidas=len(pend["secciones"]),
                              lotes_fallidos=len(pend["lotes_embeddings"]))
            resumen[final] += 1
        except ps.ErrorFatal as ex:
            sesion.actualizar(f, estado="interrumpido", error=str(ex))
            log(f"\n{ex}\nSesión detenida; el estado está guardado. "
                f"Cuando se resuelva, ejecuta: python doc_rag.py retry")
            return 2
        except KeyboardInterrupt:
            sesion.actualizar(f, estado="interrumpido", error="cancelado por el usuario")
            log("\nInterrumpido. Estado guardado: python doc_rag.py retry para continuar.")
            return 130
        except SystemExit:
            raise
        except Exception as ex:
            sesion.actualizar(f, estado="error", error=f"{type(ex).__name__}: {ex}"[:500])
            log(f"ERROR en {f}: {type(ex).__name__}: {ex}  (se continúa con el siguiente)")
            resumen["error"] += 1

    log("\n=== Sesión: " + ", ".join(f"{v} {k}" for k, v in resumen.items()))
    if resumen["incompleto"] or resumen["error"]:
        log("Hay pendientes: python doc_rag.py status para el detalle, "
            "python doc_rag.py retry para reprocesarlos.")
        return 1
    return 0


def mostrar_estado(sesion: Sesion, detalle: bool = True) -> None:
    archivos = sesion.d.get("archivos", {})
    if not archivos:
        print(f"no hay sesión registrada en {sesion.path}. Si el ingest se lanzó con otra base "
              f"de datos, indícala con --db (antes del comando) o con la variable RAG_DB.")
        return
    print(f"sesión: {sesion.path}  (actualizada {sesion.d.get('actualizado', '?')})")
    print(f"BD:     {sesion.path.with_name(sesion.path.name.replace('_sesion.json', '.db'))}\n")
    for ruta, e in sorted(archivos.items()):
        marca = {"completo": "✓", "incompleto": "⚠", "error": "✗", "interrumpido": "⏸",
                 "pendiente": "·", "en_curso": "…"}.get(e.get("estado"), "?")
        extra = []
        if e.get("secciones_fallidas"):
            extra.append(f"{e['secciones_fallidas']} secciones con error")
        if e.get("lotes_fallidos"):
            extra.append(f"{e['lotes_fallidos']} lotes de embeddings")
        print(f"{marca} {e.get('estado', '?'):12s} {e.get('nombre', Path(ruta).stem):28s} "
              f"{'; '.join(extra)}")
        if e.get("error"):
            print(f"      error: {e['error'][:200]}")
        if not detalle or e.get("estado") == "completo" or not e.get("out_dir"):
            continue
        estado_f = Path(e["out_dir"]) / "estado.json"
        if not estado_f.exists():
            continue
        pend = ps.Estado(Path(e["out_dir"])).pendientes()
        for s in pend["secciones"][:10]:
            print(f"      sección {s['id']} {s['titulo'][:45]} — {s['estado']}"
                  + (f" (falló {s.get('fallos', 1)} veces): {s.get('error', '')[:100]}"
                     if s.get("error") else ""))
        if len(pend["secciones"]) > 10:
            print(f"      … y {len(pend['secciones']) - 10} secciones más")
        for l in pend["lotes_embeddings"][:10]:
            print(f"      lote {l['lote']}: {l['registros']} registros {l['tipos']} "
                  f"secciones {l['secciones'][:6]} — {l['error'][:100]}")


# --------------------------------------------------------------------------
# Recuperación y respuesta
# --------------------------------------------------------------------------

REWRITE_SYSTEM = """You turn a user question into search queries for a technical document \
collection. Reply with JSON: {"queries": [...], "keywords": [...]}. "queries": 1-3 short, \
self-contained search queries; if the question is not in English, include an English version, \
since technical documents are often written in English. "keywords": exact identifiers, acronyms \
or technical terms worth matching literally. JSON only."""

ANSWER_SYSTEM = """You answer questions using ONLY the numbered sources provided.
- Cite every factual statement with its source number in brackets, e.g. [2] or [1][3].
- Quote requirement identifiers and numeric values exactly as they appear.
- If the sources do not contain the answer, say so clearly and state what is missing; never \
use outside knowledge to fill gaps.
- If sources contradict each other, point it out.
- Answer in the same language as the question."""


def rrf(listas: list[list[int]], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion: combina rankings sin tener que calibrar puntuaciones."""
    score: dict[int, float] = {}
    for lista in listas:
        for rank, rid in enumerate(lista):
            score[rid] = score.get(rid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda r: -score[r])


def buscar(store: Store, emb: Embedder, consulta: str, k: int = 8, document: str = "",
           kinds: list[str] | None = None, llm: ps.LLM | None = None) -> list[dict]:
    permitidos = store.filtro(document, kinds)
    consultas, extra = [consulta], []
    if llm is not None:
        try:
            d = llm.json(REWRITE_SYSTEM, consulta, {
                "type": "object", "properties": {
                    "queries": {"type": "array", "items": {"type": "string"}},
                    "keywords": {"type": "array", "items": {"type": "string"}}},
                "required": ["queries", "keywords"]}, "rewrite")
            consultas += [q for q in d.get("queries", []) if isinstance(q, str) and q.strip()][:3]
            extra = [w for w in d.get("keywords", []) if isinstance(w, str)][:8]
        except Exception as e:
            log(f"[aviso] reescritura de consulta fallida, sigo con la original: {e}")
    n = max(k * 3, 20)
    listas = [store.por_ref(" ".join([consulta] + extra), permitidos)]
    for q, qvec in zip(consultas, emb.queries(consultas)):
        listas.append(store.denso(qvec, n, permitidos))
        listas.append(store.lexico(q, n, permitidos))
    if extra:
        listas.append(store.lexico(" ".join(extra), n, permitidos))
    ids = rrf([l for l in listas if l])[:k]
    return store.registros(ids)


def cita(r: dict) -> str:
    pag = ""
    if r.get("page_start"):
        pag = f", p. {r['page_start']}" + (f"-{r['page_end']}" if r["page_end"] != r["page_start"] else "")
    sec = f" › {r['section_title']}" if r.get("section_title") else ""
    return f"{r['doc_name']}{sec}{pag} ({r['kind']})"


def montar_contexto(store: Store, hits: list[dict], max_chars: int = CONTEXT_CHARS) -> list[dict]:
    """Sustituye cada fragmento por él y sus vecinos, sin duplicar, hasta max_chars."""
    fuentes, usados, total = [], set(), 0
    for h in hits:
        grupo = [r for r in store.vecinos(h) if r["id"] not in usados]
        if not grupo:
            continue
        texto = " … ".join(r["text"] for r in grupo)
        if total + len(texto) > max_chars and fuentes:
            break
        usados.update(r["id"] for r in grupo)
        total += len(texto)
        fuentes.append({**h, "context": texto})
    return fuentes


def responder(store: Store, emb: Embedder, llm: ps.LLM, pregunta: str, k: int = 8,
              document: str = "", rewrite: bool = True) -> dict:
    hits = buscar(store, emb, pregunta, k, document, llm=llm if rewrite else None)
    fuentes = montar_contexto(store, hits)
    if not fuentes:
        return {"answer": "No hay nada indexado que se relacione con la pregunta.", "sources": []}
    bloques = "\n\n".join(f"[{i}] {cita(f)}\n{f['context']}" for i, f in enumerate(fuentes, 1))
    resp = llm.client.chat.completions.create(
        model=llm.model, temperature=llm.temperature,
        messages=[{"role": "system", "content": ANSWER_SYSTEM},
                  {"role": "user", "content": f"Sources:\n\n{bloques}\n\nQuestion: {pregunta}"}])
    return {"answer": (resp.choices[0].message.content or "").strip(),
            "sources": [{"n": i, "cite": cita(f), "doc": f["doc_name"], "section": f["section_id"],
                         "file": _fichero_seccion(f), "kind": f["kind"], "ref": f.get("ref", ""),
                         "excerpt": f["text"][:300]} for i, f in enumerate(fuentes, 1)]}


def _fichero_seccion(r: dict) -> str:
    if not r.get("out_dir") or not r.get("section_id"):
        return ""
    hits = list((Path(r["out_dir"]) / "secciones").glob(f"{r['section_id']}-*.md"))
    return str(hits[0]) if hits else ""


# --------------------------------------------------------------------------
# Modo MCP
# --------------------------------------------------------------------------

def servir_mcp(store: Store, emb: Embedder, llm: ps.LLM) -> None:
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("doc-rag", instructions=(
        "Base de conocimiento de documentación técnica indexada. Usa search_docs para "
        "localizar pasajes (devuelve texto, documento, sección y páginas), get_section para "
        "leer una sección completa, get_requirement para un ID de requisito exacto y "
        "ask_docs para una respuesta redactada con citas. Cita siempre documento y sección."))

    @mcp.tool()
    def list_documents() -> list[dict]:
        """Documentos indexados con su título y resumen."""
        return [{"name": r["name"], "title": r["title"], "source": r["source"],
                 "overview": json.loads(r["overview"] or "{}").get("summary", "")}
                for r in store.db.execute("SELECT * FROM documents ORDER BY name")]

    @mcp.tool()
    def search_docs(query: str, k: int = 8, document: str = "", kind: str = "") -> list[dict]:
        """Búsqueda híbrida (semántica + palabras clave + IDs exactos).
        kind opcional: chunk | section_summary | requirement | definition | open_issue | overview."""
        hits = buscar(store, emb, query, min(k, 20), document, [kind] if kind else None)
        return [{"cite": cita(h), "doc": h["doc_name"], "section": h["section_id"],
                 "kind": h["kind"], "ref": h.get("ref", ""), "text": h["text"]} for h in hits]

    @mcp.tool()
    def get_section(document: str, section: str) -> dict:
        """Markdown completo de una sección (resumen, requisitos, texto original)."""
        row = store.db.execute("SELECT out_dir FROM documents WHERE name = ?", (document,)).fetchone()
        if not row:
            return {"error": f"documento '{document}' no indexado"}
        f = _fichero_seccion({"out_dir": row[0], "section_id": section})
        if not f:
            return {"error": f"sección '{section}' no encontrada en {document}"}
        return {"file": f, "markdown": Path(f).read_text(encoding="utf-8")[:20000]}

    @mcp.tool()
    def get_requirement(req_id: str) -> list[dict]:
        """Texto exacto de un requisito por su ID, con documento y sección."""
        hits = store.registros([r[0] for r in store.db.execute(
            "SELECT id FROM records WHERE kind = 'requirement' AND UPPER(ref) = ?",
            (req_id.strip().upper(),))])
        return [{"cite": cita(h), "text": h["text"], "doc": h["doc_name"],
                 "section": h["section_id"]} for h in hits]

    @mcp.tool()
    def ask_docs(question: str, document: str = "") -> dict:
        """Respuesta redactada solo a partir de los documentos, con citas [n]."""
        return responder(store, emb, llm, question, document=document)

    mcp.run()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="RAG local para documentación técnica")
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="procesar e indexar uno o varios ficheros o carpetas")
    p.add_argument("inputs", type=Path, nargs="+")
    p.add_argument("--out", type=Path, default=None, help="carpeta de salida (solo con un fichero)")
    p.add_argument("--out-root", type=Path, default=None,
                   help="carpeta donde crear <nombre>_estructurado/ (por defecto, junto a la BD; "
                        "los ficheros ya conocidos conservan su carpeta)")
    p.add_argument("--name", default=None, help="nombre en el índice (solo con un fichero)")
    p.add_argument("--lang", default="")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--max-chars", type=int, default=12000)
    p.add_argument("--heading-regex", default=None)
    p.add_argument("--estructura", choices=ps.ESTRUCTURAS, default="auto",
                   help="secciones por marcadores del PDF, títulos o tamaño fijo")
    p.add_argument("--min-chars", type=int, default=300)
    p.add_argument("--force", action="store_true", help="reprocesar aunque esté al día")

    p = sub.add_parser("retry", help="reprocesar los ficheros con pendientes de la sesión")
    p.add_argument("--all", action="store_true", help="incluir también los completos")
    p.add_argument("--workers", type=int, default=None, help="cambiar los hilos de análisis")

    p = sub.add_parser("status", help="estado de la sesión de ingesta")
    p.add_argument("--resumen", action="store_true", help="sin detalle de secciones y lotes")

    p = sub.add_parser("index", help="indexar una carpeta ya generada por pdf_struct.py")
    p.add_argument("folder", type=Path)
    p.add_argument("--name", default=None)

    p = sub.add_parser("search", help="buscar pasajes")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--doc", default="")
    p.add_argument("--kind", default="")
    p.add_argument("--rewrite", action="store_true", help="reescribir la consulta con el LLM")

    p = sub.add_parser("ask", help="pregunta con respuesta citada")
    p.add_argument("question")
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--doc", default="")
    p.add_argument("--no-rewrite", action="store_true")

    sub.add_parser("docs", help="listar documentos indexados")
    sub.add_parser("mcp", help="servidor MCP por stdio")
    sub.add_parser("check", help="probar conexión con el LLM y el embedder (proxy, TLS)")
    cx.añadir_args(ap, embeddings=True)
    args = ap.parse_args()

    if args.cmd == "status":
        mostrar_estado(Sesion(args.db), detalle=not args.resumen)
        return
    conn = cx.cargar(args)
    if args.cmd == "check":
        sys.exit(cx.comprobar(conn))

    store = Store(args.db)
    emb = Embedder(conn.embeddings)
    llm = lambda: ps.LLM(conn.llm)  # noqa: E731

    if args.cmd == "ingest":
        archivos = expandir(args.inputs)
        if not archivos:
            sys.exit("no hay ficheros que procesar")
        if len(archivos) > 1 and (args.out or args.name):
            sys.exit("--out y --name solo valen con un único fichero; usa --out-root")
        sesion = Sesion(args.db)
        nombres = nombres_unicos(archivos, sesion)
        out_root = (args.out_root or Path(args.db).expanduser().resolve().parent)
        opciones = {}
        for f in archivos:
            previa = sesion.conocida(f) or {}
            out_dir = (args.out or (Path(previa["out_dir"]) if previa.get("out_dir") and not args.out_root
                                    else out_root / f"{nombres[f]}_estructurado"))
            opciones[f] = {"nombre": args.name or nombres[f], "out_dir": str(Path(out_dir).resolve()),
                           "lang": args.lang, "workers": args.workers, "max_chars": args.max_chars,
                           "min_chars": args.min_chars, "heading_regex": args.heading_regex,
                           "estructura": args.estructura}
        log(f"BD: {Path(args.db).resolve()}  sesión: {sesion.path}")
        sys.exit(ingest_archivos(archivos, opciones, conn, store, emb, sesion, args.force))
    elif args.cmd == "retry":
        sesion = Sesion(args.db)
        log(f"BD: {Path(args.db).resolve()}  sesión: {sesion.path}")
        if not sesion.path.exists():
            sys.exit(f"no hay sesión en {sesion.path}. Si el ingest se lanzó con otra base de "
                     f"datos, indícala con --db (antes del comando) o con la variable RAG_DB.")
        archivos = [Path(r) for r, e in sesion.d.get("archivos", {}).items()
                    if args.all or e.get("estado") != "completo"]
        if not archivos:
            print("no hay nada pendiente en la sesión")
            return
        faltan = [f for f in archivos if not f.exists()]
        for f in faltan:
            log(f"[aviso] ya no existe {f}: se omite")
        archivos = [f for f in archivos if f.exists()]
        opciones = {}
        for f in archivos:
            e = sesion.entrada(f)
            opciones[f] = {"nombre": e.get("nombre", f.stem),
                           "out_dir": e.get("out_dir", str(Path(f"{f.stem}_estructurado").resolve())),
                           "lang": e.get("lang", ""),
                           "workers": args.workers or e.get("workers", 1),
                           "max_chars": e.get("max_chars", 12000),
                           "min_chars": e.get("min_chars", 300),
                           "heading_regex": e.get("heading_regex"),
                           "estructura": e.get("estructura", "auto")}
        log(f"reintentando {len(archivos)} ficheros con pendientes")
        sys.exit(ingest_archivos(archivos, opciones, conn, store, emb, sesion, force=True))
    elif args.cmd == "status":
        mostrar_estado(Sesion(args.db), detalle=not args.resumen)
    elif args.cmd == "index":
        f = args.folder / "estructura.json"
        if not f.exists():
            sys.exit(f"no encuentro {f}")
        datos = json.loads(f.read_text(encoding="utf-8"))
        datos.setdefault("out_dir", str(args.folder.resolve()))
        try:
            completo = indexar(store, emb, datos, args.name, ps.Estado(args.folder))
        except ps.ErrorFatal as e:
            sys.exit(f"{e}\nEstado guardado: vuelve a ejecutar el mismo comando para continuar.")
        sys.exit(0 if completo else 1)
    elif args.cmd == "search":
        hits = buscar(store, emb, args.query, args.k, args.doc,
                      [args.kind] if args.kind else None, llm() if args.rewrite else None)
        for i, h in enumerate(hits, 1):
            print(f"\n[{i}] {cita(h)}\n    {h['text'][:400]}")
    elif args.cmd == "ask":
        res = responder(store, emb, llm(), args.question, args.k, args.doc, not args.no_rewrite)
        print(res["answer"])
        print("\nFuentes:")
        for s in res["sources"]:
            print(f"  [{s['n']}] {s['cite']}" + (f"\n       {s['file']}" if s["file"] else ""))
    elif args.cmd == "docs":
        for r in store.db.execute("SELECT d.*, COUNT(r.id) AS n FROM documents d LEFT JOIN "
                                  "records r ON r.doc_id = d.id GROUP BY d.id ORDER BY d.name"):
            print(f"{r['name']:30s} {r['n']:6d} registros  {r['title']}  ({r['ingested_at']})")
    elif args.cmd == "mcp":
        servir_mcp(store, emb, llm())


if __name__ == "__main__":
    main()
