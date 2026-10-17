"""Usage log of the workspace: one row per tool call of the MCP server, per tool call of an
agent and per command of the CLI, in the `usage` table of atlas.db.

    log = usage_sink(cfg, "mcp")                  # None with [mcp] usage_log = "off"
    tool = plugins.instrumented(tool, log)        # each call, a row
    summary(cfg.workspace / "atlas.db", days=30)  # what `atlas usage` prints

A row has the date, the origin (mcp | cli | agent), the command the process is running
(atlas.llm.usage_label), the tool or command, its step or agent, the milliseconds, ok or
error, the status of an answer (answered | not_found, or its own `status`), and what the
call asked of the servers: requests, tokens and cost (what atlas.llm.usage() grew during
it; calls that run at the same time in one process share it). With usage_log = "counts"
nothing of the arguments is kept; with "full", the arguments as JSON, cut at ARGS_CHARS.

A table and not a .jsonl: `atlas run` and `atlas serve` write at the same time over the
same workspace, and SQLite serialises them (each row is its own short transaction). Nothing
of this leaves the computer.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = ["ARGS_CHARS", "MODES", "UsageLog", "answer_status", "parse_since", "summary", "usage_sink"]

log = logging.getLogger(__name__)

MODES = ("counts", "full", "off")
ARGS_CHARS = 200          # characters of the arguments kept with usage_log = "full"
TOKENS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens", "embedding_tokens")

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY, at TEXT, origin TEXT, command TEXT, name TEXT, detail TEXT, ms INTEGER,
    outcome TEXT, status TEXT, requests INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
    reasoning_tokens INTEGER, cached_tokens INTEGER, embedding_tokens INTEGER, cost REAL, error TEXT,
    args TEXT);
CREATE INDEX IF NOT EXISTS idx_usage_at ON usage(at);
"""
_READY: set[str] = set()          # databases whose table this process has created
_READY_LOCK = threading.Lock()


def _connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    with _READY_LOCK:
        if str(db) not in _READY:
            con.executescript(SCHEMA)
            _READY.add(str(db))
    return con


def answer_status(result: Any) -> str | None:
    """The status of a result: its own `status` if it has one, `error` if it is an error
    answer, and for an answer with sources (ask, ask_docs), answered or not_found."""
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("status"), str):
        return result["status"]
    if "error" in result:
        return "error"
    if isinstance(result.get("sources"), list):
        return "answered" if result["sources"] else "not_found"
    return None


class UsageLog:
    """A sink of plugins.instrumented: each event, a row of the usage table of `db`."""

    def __init__(self, db: str | Path, mode: str = "counts", origin: str = "cli", detail: str = ""):
        if mode not in MODES:
            raise ValueError(f"usage_log must be one of {', '.join(MODES)}")
        self.db, self.mode, self.origin, self.detail = Path(db), mode, origin, detail

    def __call__(self, event: dict) -> None:
        if self.mode == "off":
            return
        from atlas.llm.connection import usage_label
        grown, result = event.get("usage") or {}, event.get("result")
        failed = bool(event.get("error")) or (isinstance(result, dict) and "error" in result)
        error = event.get("error") or (str(result["error"])[:500] if failed and isinstance(result, dict) else None)
        args = None
        if self.mode == "full":
            args = json.dumps(event.get("args") or {}, ensure_ascii=False, default=str)[:ARGS_CHARS]
        row = (datetime.now().isoformat(timespec="seconds"), self.origin, usage_label()[0], event["name"],
               event.get("detail", self.detail), round(event.get("seconds", 0) * 1000), "error" if failed else "ok",
               answer_status(result), int(sum(grown.get(k, 0) for k in ("chat", "embeddings", "other"))),
               *(int(grown.get(k, 0)) for k in TOKENS), round(grown.get("cost", 0), 6), error, args)
        con = _connect(self.db)
        try:
            with con:
                con.execute(f"INSERT INTO usage (at, origin, command, name, detail, ms, outcome, status, requests, "
                            f"{', '.join(TOKENS)}, cost, error, args) VALUES ({', '.join('?' * len(row))})", row)
        finally:
            con.close()


def usage_sink(cfg, origin: str, detail: str = "") -> UsageLog | None:
    """The usage log of the workspace of `cfg` for calls of `origin` (mcp | cli | agent), as
    [mcp] usage_log says; None when it is "off"."""
    mode = cfg["mcp"].get("usage_log", "counts")
    return None if mode == "off" else UsageLog(cfg.workspace / "atlas.db", mode, origin, detail)


def parse_since(text: str) -> int:
    """Days of '30d', '4w', '2m' (months of 30 days) or '30'."""
    t = (text or "").strip().lower()
    factor = {"d": 1, "w": 7, "m": 30}.get(t[-1:], None)
    try:
        days = int(t[:-1] if factor else t) * (factor or 1)
    except ValueError:
        raise ValueError(f"--since must be a number of days, weeks or months: 30d, 4w, 2m (not {text!r})") from None
    if days < 1:
        raise ValueError("--since must be at least one day")
    return days


def _week(at: str) -> str:
    year, week, _ = datetime.fromisoformat(at).isocalendar()
    return f"{year}-W{week:02d}"


def summary(db: str | Path, days: int = 30) -> dict:
    """What the usage log of `db` says of the last `days` days:
    {"since", "calls": total, "weeks": [...], "by_name": [{origin, name, calls, errors,
    median_ms, requests, *TOKENS, cost, weeks: {week: calls}}], "answers": {name: {status: n}},
    "total": {requests, *TOKENS, cost}}. The total adds the rows of the CLI and of the MCP
    server: the tool calls of an agent are already inside the command or tool that ran it."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    out: dict = {"since": since[:10], "calls": 0, "weeks": [], "by_name": [], "answers": {},
                 "total": {"requests": 0, **{k: 0 for k in TOKENS}, "cost": 0.0}}
    if not Path(db).exists():
        return out
    con = _connect(Path(db))
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM usage WHERE at >= ? ORDER BY at", (since,))]
    finally:
        con.close()
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["origin"], r["name"]), []).append(r)
        if r["status"] in ("answered", "not_found", "partial"):
            counts = out["answers"].setdefault(r["name"], {})
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["origin"] in ("cli", "mcp"):
            for k in out["total"]:
                out["total"][k] += r[k] or 0
    out["total"]["cost"] = round(out["total"]["cost"], 6)
    weeks: set[str] = set()
    for (origin, name), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]), kv[0][1])):
        by_week: dict[str, int] = {}
        for r in rs:
            w = _week(r["at"])
            by_week[w] = by_week.get(w, 0) + 1
        weeks |= set(by_week)
        out["by_name"].append({"origin": origin, "name": name, "calls": len(rs),
                               "errors": sum(r["outcome"] == "error" for r in rs),
                               "median_ms": round(statistics.median(r["ms"] or 0 for r in rs)),
                               "requests": sum(r["requests"] or 0 for r in rs),
                               **{k: sum(r[k] or 0 for r in rs) for k in TOKENS},
                               "cost": round(sum(r["cost"] or 0 for r in rs), 6), "weeks": by_week})
    out["calls"], out["weeks"] = len(rows), sorted(weeks)
    return out
