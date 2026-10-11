"""Requirements adapters: how atlas reads requirements.

"exchange" (the usual one) reads the requirements exchange database of
atlas.doors.exchange: a fixed schema that a filler fills (the DOORS export of
tools/doors_export, or any other import). Nothing of the source is configured here:

    [sources.doors]
    adapter = "exchange"
    modules = []                           # empty = every module of the database

    [sources.doors.exchange]
    path = "C:/atlas/requirements.sqlite"
    kinds = ["requirement", "heading", "information"]   # which objects are read

    [sources.doors.convention]             # how IDs match documents and code
    id_pattern = "^(?:SRS|SSS)-\\d{3,6}$"
    inline_reference_regex = "\\b(?:SRS|SSS)-\\d{3,6}\\b"

"database" reads any other database with configured queries and columns:

    [sources.doors]
    adapter = "database"
    modules = ["SRS_TMS"]                  # empty = every module returned by modules_query

    [sources.doors.database]
    driver = "sqlite"                      # sqlite | odbc  (odbc: pip install pyodbc, uses dsn)
    path = "examples/samples/doors.sqlite"
    modules_query = "SELECT DISTINCT module FROM requirements ORDER BY module"
    requirements_query = "SELECT * FROM requirements WHERE module = :module ORDER BY id"
    links_query = "SELECT source_id, target_id, link_type FROM links WHERE source_module = :module"
    snapshot_query = "SELECT COUNT(*) || '|' || MAX(updated_at) FROM requirements WHERE module = :module"

    [sources.doors.convention]
    id_column = "id"
    text_column = "text"
    parent_column = "parent"               # optional: hierarchy inside the module
    attribute_columns = ["status", "verification"]
    id_pattern = "^(?:SRS|SSS)-\\d{3,6}$"   # IDs that do not match are reported
    id_normalize = "upper"                 # upper | none (spaces are always removed)
    link_columns = { source = "source_id", target = "target_id", type = "link_type" }
    inline_reference_regex = "\\b(?:SRS|SSS)-\\d{3,6}\\b"   # references inside the text
    inline_reference_type = "references"

Use as a library:
    from atlas.doors import ExchangeAdapter, diff_requirements
    ad = ExchangeAdapter(cfg["sources"]["doors"])
    reqs = list(ad.requirements("SRS_TMS"))
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from atlas.doors.exchange import KINDS, open_exchange
from atlas.plugins import Link, Requirement, RequirementsAdapter, register

log = logging.getLogger(__name__)

DEFAULT_CONVENTION = {
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
    """previous: id -> stored hash. Returns the added, changed and removed IDs."""
    now = {r.id: hash_requirement(r) for r in current}
    return {
        "added": sorted(i for i in now if i not in previous),
        "changed": sorted(i for i in now if i in previous and previous[i] != now[i]),
        "removed": sorted(i for i in previous if i not in now),
    }


class DatabaseAdapter(RequirementsAdapter):
    name = "database"

    def __init__(self, config: dict):
        super().__init__(config)
        db = config.get("database") or {}
        self.driver = db.get("driver", "sqlite")
        self.db_cfg = db
        self.conv = {**DEFAULT_CONVENTION, **(config.get("convention") or {})}
        self.conv["link_columns"] = {**DEFAULT_CONVENTION["link_columns"], **self.conv.get("link_columns", {})}
        missing = [k for k in ("modules_query", "requirements_query") if not db.get(k)]
        if missing:
            raise ValueError(f"[sources.doors.database] missing: {', '.join(missing)}")
        self._id_re = re.compile(self.conv["id_pattern"]) if self.conv.get("id_pattern") else None
        self._ref_re = (re.compile(self.conv["inline_reference_regex"])
                        if self.conv.get("inline_reference_regex") else None)
        self._conn = None

    # --- connection -------------------------------------------------------
    def _connect(self):
        if self._conn is not None:
            return self._conn
        if self.driver == "sqlite":
            path = Path(self.db_cfg["path"]).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"DOORS database {path} does not exist")
            self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
        elif self.driver == "odbc":
            try:
                import pyodbc  # type: ignore
            except ImportError:
                raise ImportError("odbc driver: pip install pyodbc") from None
            self._conn = pyodbc.connect(self.db_cfg["dsn"])
        else:
            raise ValueError(f"unsupported database driver: {self.driver}")
        return self._conn

    def _rows(self, query: str, params: dict) -> list[dict]:
        conn = self._connect()
        if self.driver == "sqlite":
            return [dict(r) for r in conn.execute(query, params)]
        cur = conn.cursor()
        q = re.sub(r":(\w+)", "?", query)               # pyodbc uses '?'
        order = re.findall(r":(\w+)", query)
        cur.execute(q, [params[k] for k in order])
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # --- interface --------------------------------------------------------
    def modules(self) -> list[str]:
        requested = self.config.get("modules") or []
        found = [str(list(r.values())[0]) for r in self._rows(self.db_cfg["modules_query"], {})]
        if not requested:
            return found
        unknown = [m for m in requested if m not in found]
        if unknown:
            log.warning("modules not found in the database: %s", unknown)
        return [m for m in requested if m in found]

    def snapshot(self, module: str) -> str:
        q = self.db_cfg.get("snapshot_query")
        if q:
            r = self._rows(q, {"module": module})
            return str(list(r[0].values())[0]) if r else ""
        # no snapshot query: hash of the whole content (slower but correct)
        return hashlib.sha256("".join(hash_requirement(x) for x in self.requirements(module)).encode()).hexdigest()[:16]

    def requirements(self, module: str) -> Iterable[Requirement]:
        c = self.conv
        for row in self._rows(self.db_cfg["requirements_query"], {"module": module}):
            raw = row.get(c["id_column"])
            if raw is None or str(raw).strip() == "":
                continue
            rid = normalize_id(raw, c["id_normalize"])
            if self._id_re and not self._id_re.match(rid):
                log.warning("ID outside the convention (%s): %r in %s", c["id_pattern"], raw, module)
            parent = row.get(c["parent_column"]) if c.get("parent_column") else None
            yield Requirement(
                id=rid, raw_id=str(raw),
                text=" ".join(str(row.get(c["text_column"]) or "").split()),
                module=str(row.get(c["module_column"]) or module),
                parent=normalize_id(parent, c["id_normalize"]) if parent else None,
                attributes={k: row.get(k) for k in c.get("attribute_columns", []) if k in row},
            )

    def links(self, module: str) -> Iterable[Link]:
        c, lc = self.conv, self.conv["link_columns"]
        if self.db_cfg.get("links_query"):
            for row in self._rows(self.db_cfg["links_query"], {"module": module}):
                yield Link(source=normalize_id(row[lc["source"]], c["id_normalize"]),
                           target=normalize_id(row[lc["target"]], c["id_normalize"]),
                           type=str(row.get(lc["type"]) or "link"))
        yield from _inline_links(self, module)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _inline_links(adapter, module: str) -> Iterable[Link]:
    """Links for the IDs written inside the text of the requirements ("see SSS-0012")."""
    if adapter._ref_re:
        for r in adapter.requirements(module):
            for ref in set(adapter._ref_re.findall(r.text)):
                ref = normalize_id(ref, adapter.conv["id_normalize"])
                if ref != r.id:
                    yield Link(source=r.id, target=ref, type=adapter.conv["inline_reference_type"])


class ExchangeAdapter(RequirementsAdapter):
    """Reads the requirements exchange database (atlas.doors.exchange). Only the matching
    rules are configured ([sources.doors.convention]: id_normalize, id_pattern and the
    references inside the text): the mapping from the source is the filler's job."""
    name = "exchange"

    def __init__(self, config: dict):
        super().__init__(config)
        ex = config.get("exchange") or {}
        if not ex.get("path"):
            raise ValueError("[sources.doors.exchange] path is missing")
        self.path = ex["path"]
        self.kinds = list(ex.get("kinds") or KINDS)
        unknown = sorted(set(self.kinds) - set(KINDS))
        if unknown:
            raise ValueError(f"[sources.doors.exchange] unknown kinds {unknown} (valid: {', '.join(KINDS)})")
        self.conv = {**DEFAULT_CONVENTION, **(config.get("convention") or {})}
        self._id_re = re.compile(self.conv["id_pattern"]) if self.conv.get("id_pattern") else None
        self._ref_re = (re.compile(self.conv["inline_reference_regex"])
                        if self.conv.get("inline_reference_regex") else None)
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = open_exchange(self.path)
        return self._conn

    def modules(self) -> list[str]:
        found = [r[0] for r in self._db().execute("SELECT module FROM exchange_modules ORDER BY module")]
        requested = self.config.get("modules") or []
        unknown = [m for m in requested if m not in found]
        if unknown:
            log.warning("modules not found in the exchange database: %s", unknown)
        return [m for m in requested if m in found] if requested else found

    def snapshot(self, module: str) -> str:
        row = self._db().execute("SELECT snapshot FROM exchange_modules WHERE module = ?", (module,)).fetchone()
        return row[0] if row else ""

    def requirements(self, module: str) -> Iterable[Requirement]:
        norm = lambda x: normalize_id(x, self.conv["id_normalize"])
        rows = self._db().execute(
            f"SELECT * FROM exchange_requirements WHERE module = ? AND kind IN ({','.join('?' * len(self.kinds))}) "
            f"ORDER BY position", (module, *self.kinds))
        for row in rows:
            rid = norm(row["id"])
            if self._id_re and not self._id_re.match(rid):
                log.warning("ID outside the convention (%s): %r in %s", self.conv["id_pattern"], row["id"], module)
            attributes = {**json.loads(row["attributes"] or "{}"), "kind": row["kind"], "heading": row["heading"],
                          "outline": row["outline"], "level": row["level"]}
            yield Requirement(id=rid, raw_id=row["id"], module=module,
                              text=" ".join((row["text"] or row["heading"] or "").split()),
                              parent=norm(row["parent"]) if row["parent"] else None, attributes=attributes)

    def links(self, module: str) -> Iterable[Link]:
        norm = lambda x: normalize_id(x, self.conv["id_normalize"])
        for row in self._db().execute("SELECT source, target, type FROM exchange_links WHERE module = ? "
                                      "ORDER BY source, target, type", (module,)):
            yield Link(source=norm(row["source"]), target=norm(row["target"]), type=row["type"])
        yield from _inline_links(self, module)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


register("requirements", "database", DatabaseAdapter)
register("requirements", "exchange", ExchangeAdapter)


def requirement_to_dict(r: Requirement) -> dict:
    return asdict(r)
