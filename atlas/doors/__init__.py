"""Requisitos de DOORS leídos desde una base de datos (explotación/exportación).

Todo lo específico de la exportación se configura, no se programa:

    [sources.doors]
    adapter = "database"
    modules = ["SRS_TMS"]                  # vacío = todos los que devuelva modules_query

    [sources.doors.database]
    driver = "sqlite"                      # sqlite | odbc  (odbc: pip install pyodbc, usa dsn)
    path = "examples/samples/doors.sqlite"
    modules_query = "SELECT DISTINCT module FROM requirements ORDER BY module"
    requirements_query = "SELECT * FROM requirements WHERE module = :module ORDER BY id"
    links_query = "SELECT source_id, target_id, link_type FROM links WHERE source_module = :module"
    snapshot_query = "SELECT COUNT(*) || '|' || MAX(updated_at) FROM requirements WHERE module = :module"

    [sources.doors.convention]
    id_column = "id"
    text_column = "text"
    parent_column = "parent"               # opcional: jerarquía dentro del módulo
    attribute_columns = ["status", "verification"]
    id_pattern = "^(?:SRS|SSS)-\\d{3,6}$"   # los IDs que no cumplen se avisan
    id_normalize = "upper"                 # upper | none (siempre se quitan espacios)
    link_columns = { source = "source_id", target = "target_id", type = "link_type" }
    inline_reference_regex = "\\b(?:SRS|SSS)-\\d{3,6}\\b"   # referencias dentro del texto
    inline_reference_type = "references"

Uso como librería:
    from atlas.doors import DatabaseAdapter, diff_requirements
    ad = DatabaseAdapter(cfg["sources"]["doors"])
    reqs = list(ad.requirements("SRS_TMS"))
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from atlas.plugins import Link, Requirement, RequirementsAdapter, register

log = logging.getLogger(__name__)

CONVENCION_DEFECTO = {
    "id_column": "id", "text_column": "text", "module_column": "module", "parent_column": "",
    "attribute_columns": [], "id_pattern": "", "id_normalize": "upper",
    "link_columns": {"source": "source_id", "target": "target_id", "type": "link_type"},
    "inline_reference_regex": "", "inline_reference_type": "references",
}


def normalize_id(raw: str, mode: str = "upper") -> str:
    s = " ".join(str(raw).split()).replace(" ", "")
    return s.upper() if mode == "upper" else s


def hash_requirement(r: Requirement) -> str:
    base = f"{r.id}|{r.text}|{r.module}|{r.parent or ''}|{sorted(r.attributes.items())}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def diff_requirements(previous: dict[str, str], current: Iterable[Requirement]) -> dict[str, list]:
    """previous: id -> hash guardado. Devuelve añadidos, cambiados y eliminados."""
    actuales = {r.id: hash_requirement(r) for r in current}
    return {
        "added": sorted(i for i in actuales if i not in previous),
        "changed": sorted(i for i in actuales if i in previous and previous[i] != actuales[i]),
        "removed": sorted(i for i in previous if i not in actuales),
    }


class DatabaseAdapter(RequirementsAdapter):
    name = "database"

    def __init__(self, config: dict):
        super().__init__(config)
        db = config.get("database") or {}
        self.driver = db.get("driver", "sqlite")
        self.db_cfg = db
        self.conv = {**CONVENCION_DEFECTO, **(config.get("convention") or {})}
        self.conv["link_columns"] = {**CONVENCION_DEFECTO["link_columns"], **self.conv.get("link_columns", {})}
        faltan = [k for k in ("modules_query", "requirements_query") if not db.get(k)]
        if faltan:
            raise ValueError(f"[sources.doors.database] faltan: {', '.join(faltan)}")
        self._id_re = re.compile(self.conv["id_pattern"]) if self.conv.get("id_pattern") else None
        self._ref_re = (re.compile(self.conv["inline_reference_regex"])
                        if self.conv.get("inline_reference_regex") else None)
        self._conn = None

    # --- conexión ---------------------------------------------------------
    def _connect(self):
        if self._conn is not None:
            return self._conn
        if self.driver == "sqlite":
            path = Path(self.db_cfg["path"]).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"no existe la base de datos de DOORS {path}")
            self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
        elif self.driver == "odbc":
            try:
                import pyodbc  # type: ignore
            except ImportError:
                raise ImportError("driver odbc: pip install pyodbc") from None
            self._conn = pyodbc.connect(self.db_cfg["dsn"])
        else:
            raise ValueError(f"driver de base de datos no soportado: {self.driver}")
        return self._conn

    def _rows(self, query: str, params: dict) -> list[dict]:
        conn = self._connect()
        if self.driver == "sqlite":
            return [dict(r) for r in conn.execute(query, params)]
        cur = conn.cursor()
        q = re.sub(r":(\w+)", "?", query)               # pyodbc usa '?'
        orden = re.findall(r":(\w+)", query)
        cur.execute(q, [params[k] for k in orden])
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # --- interfaz ---------------------------------------------------------
    def modules(self) -> list[str]:
        pedidos = self.config.get("modules") or []
        todos = [str(list(r.values())[0]) for r in self._rows(self.db_cfg["modules_query"], {})]
        if not pedidos:
            return todos
        desconocidos = [m for m in pedidos if m not in todos]
        if desconocidos:
            log.warning("módulos no encontrados en la base de datos: %s", desconocidos)
        return [m for m in pedidos if m in todos]

    def snapshot(self, module: str) -> str:
        q = self.db_cfg.get("snapshot_query")
        if q:
            r = self._rows(q, {"module": module})
            return str(list(r[0].values())[0]) if r else ""
        # sin consulta de instantánea: hash de todo el contenido (más lento pero correcto)
        return hashlib.sha256("".join(hash_requirement(x) for x in self.requirements(module)).encode()).hexdigest()[:16]

    def requirements(self, module: str) -> Iterable[Requirement]:
        c = self.conv
        for fila in self._rows(self.db_cfg["requirements_query"], {"module": module}):
            raw = fila.get(c["id_column"])
            if raw is None or str(raw).strip() == "":
                continue
            rid = normalize_id(raw, c["id_normalize"])
            if self._id_re and not self._id_re.match(rid):
                log.warning("ID fuera de la convención (%s): %r en %s", c["id_pattern"], raw, module)
            parent = fila.get(c["parent_column"]) if c.get("parent_column") else None
            yield Requirement(
                id=rid, raw_id=str(raw),
                text=" ".join(str(fila.get(c["text_column"]) or "").split()),
                module=str(fila.get(c["module_column"]) or module),
                parent=normalize_id(parent, c["id_normalize"]) if parent else None,
                attributes={k: fila.get(k) for k in c.get("attribute_columns", []) if k in fila},
            )

    def links(self, module: str) -> Iterable[Link]:
        c, lc = self.conv, self.conv["link_columns"]
        if self.db_cfg.get("links_query"):
            for fila in self._rows(self.db_cfg["links_query"], {"module": module}):
                yield Link(source=normalize_id(fila[lc["source"]], c["id_normalize"]),
                           target=normalize_id(fila[lc["target"]], c["id_normalize"]),
                           type=str(fila.get(lc["type"]) or "link"))
        if self._ref_re:                     # referencias escritas dentro del texto
            for r in self.requirements(module):
                for ref in set(self._ref_re.findall(r.text)):
                    ref = normalize_id(ref, c["id_normalize"])
                    if ref != r.id:
                        yield Link(source=r.id, target=ref, type=c["inline_reference_type"])

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


register("requirements", "database", DatabaseAdapter)


def requirement_to_dict(r: Requirement) -> dict:
    return asdict(r)
