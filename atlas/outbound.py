"""What goes out to the models: the sources whose text never leaves the workstation, and the
record of what has been sent to each model.

Local-only sources (`model = "none"`): a [[sources.docs]], some or all DOORS modules
([sources.doors] model, local_only) and the code ([sources.code] model). Their text is never
analysed by the model nor embedded; it is in the graph and in the lexical and ID searches,
and the tools that do not call a model (search, get_section, trace, read_source,
build_context through MCP) give it whole. What calls a model leaves it out of the prompt and
says what it left out (`local_only`).

    local = LocalOnly.from_config(cfg)
    local.index_doc("doors:SRS_CLIENT")      # True if that module is local-only
    local.node(G, "tracks.delete_track")     # a node of the graph

Identifiers (requirement IDs, code names, file names) are not text of a source: they may go
in a prompt to say what was left out.

The record (table `sent` of atlas.db): by model, source and kind (sections analysed, texts
embedded, answers, judgements, reports, agent runs), how many and when the last one went.
It is written where the text is sent, so it counts from the first run of a version that has
it; atlas status and the report of atlas run show it.

    record_sent(cfg.workspace / "atlas.db", "gemma", {"docs:manuals": 3}, "sections")
    sent_summary(cfg.workspace / "atlas.db")  # [{"model", "source", "kind", "count", "last"}]
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = ["MODELS", "LocalOnly", "record_sent", "sent_summary", "sent_totals", "source_label"]

MODELS = ("full", "none")          # model = ...: "none" keeps the text of a source in the workstation
DOORS_PREFIX = "doors:"            # the keys of the index (atlas.index.records)
CODE_PREFIX = "code:"
CODE_KINDS = ("subprogram", "test", "type", "variable", "constant", "unit", "external")
REQUIREMENT_KINDS = ("requirement", "heading", "information")


def source_label(doc: str) -> str:
    """The source of a document of the index, as the record names it: "docs:<source>" for a
    document (its key is "<source>:<path>"), "doors:<module>", or "code"."""
    if doc.startswith(DOORS_PREFIX):
        return doc
    if doc.startswith(CODE_PREFIX):
        return "code"
    return "docs:" + doc.split(":", 1)[0]


@dataclass(frozen=True)
class LocalOnly:
    """The local-only sources of a configuration."""
    docs: frozenset = field(default_factory=frozenset)       # names of [[sources.docs]]
    modules: frozenset = field(default_factory=frozenset)    # DOORS modules
    all_modules: bool = False                                 # [sources.doors] model = "none"
    code: bool = False                                        # [sources.code] model = "none"

    @classmethod
    def from_config(cls, cfg) -> "LocalOnly":
        sources = cfg["sources"] if cfg is not None else {}
        doors, code = sources.get("doors") or {}, sources.get("code") or {}
        return cls(docs=frozenset(d["name"] for d in sources.get("docs", []) if d.get("model") == "none"),
                   modules=frozenset(doors.get("local_only", [])), all_modules=doors.get("model") == "none",
                   code=code.get("model") == "none")

    def __bool__(self) -> bool:
        return bool(self.docs or self.modules or self.all_modules or self.code)

    def module(self, module: str | None) -> bool:
        return bool(module) and (self.all_modules or module in self.modules)

    def source(self, name: str) -> bool:
        """A [[sources.docs]] by its name."""
        return name in self.docs

    def index_doc(self, doc: str) -> bool:
        """A document of the index by its key: "<source>:<path>", "doors:<module>", "code:<unit>"."""
        if doc.startswith(DOORS_PREFIX):
            return self.module(doc[len(DOORS_PREFIX):])
        if doc.startswith(CODE_PREFIX):
            return self.code
        return self.source(doc.split(":", 1)[0])

    def node(self, G, n: str) -> bool:
        """A node of the knowledge graph: a requirement of a local module, a document or
        section of a local source, or code when the code is local."""
        d = G.nodes[n]
        kind = d.get("kind")
        if kind in REQUIREMENT_KINDS:
            return self.module(d.get("module"))
        if kind == "document":
            return self.source(d.get("source", ""))
        if kind == "section":
            doc = f"doc:{d.get('document')}"
            return doc in G and self.source(G.nodes[doc].get("source", ""))
        return self.code and kind in CODE_KINDS

    def describe(self) -> list[str]:
        """The local-only sources, for a person."""
        out = [f"docs:{n}" for n in sorted(self.docs)]
        out += ["doors (every module)"] if self.all_modules else [f"doors:{m}" for m in sorted(self.modules)]
        return out + (["code"] if self.code else [])


# --- the record of what was sent --------------------------------------------------------

SCHEMA = """CREATE TABLE IF NOT EXISTS sent (
    model TEXT, source TEXT, kind TEXT, count INTEGER, last TEXT, PRIMARY KEY (model, source, kind))"""
_LOCK = threading.Lock()


def _connect(db: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(Path(db), timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute(SCHEMA)
    return con


def record_sent(db: str | Path | None, model: str, counts: Mapping[str, int] | Iterable[str], kind: str) -> None:
    """Adds to the record of atlas.db that `model` was sent `counts` ({source: n}, or the
    sources, one each) of `kind`. Never fails: the record must not stop the work."""
    if not db or not model:
        return
    if not isinstance(counts, Mapping):
        counts = {s: 1 for s in counts}
    rows = [(model, source, kind, int(n)) for source, n in counts.items() if n]
    if not rows:
        return
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with _LOCK:
            con = _connect(db)
            try:
                with con:
                    con.executemany("INSERT INTO sent VALUES (?,?,?,?,?) ON CONFLICT(model, source, kind) DO UPDATE "
                                    "SET count = count + excluded.count, last = excluded.last",
                                    [(*r, now) for r in rows])
            finally:
                con.close()
    except sqlite3.Error as e:
        import logging
        logging.getLogger(__name__).warning("the record of what was sent could not be written: %s", e)


def sent_summary(db: str | Path) -> list[dict[str, Any]]:
    """Every row of the record, by model, source and kind."""
    if not Path(db).exists():
        return []
    con = _connect(db)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM sent ORDER BY model, source, kind")]
    finally:
        con.close()


def sent_totals(db: str | Path) -> dict[tuple[str, str, str], int]:
    """(model, source, kind) -> count: to subtract two moments (what a run sent)."""
    return {(r["model"], r["source"], r["kind"]): r["count"] for r in sent_summary(db)}
