"""Incremental, recoverable processing engine.

A `Step` declares its units, how to compute the fingerprint of each one and how to
process it. The `Runner` checks the `Ledger` (SQLite): if the fingerprint has not changed
and the unit is ok, it skips it; otherwise it processes it. Errors of a single unit are
recorded and the run goes on; fatal ones (connection, credentials, exhausted quota) stop
the run with the state saved. Running again resumes. Units that disappear are marked
stale and, optionally, pruned.

Each run leaves a time profile in its report and in the `runs` table of the ledger: per
step, the seconds spent finding the work (units and fingerprints) and doing it, the slowest
units and, with `meters`, what the step consumed of anything that counts (requests to the
APIs, seconds waiting for the rate limit...).

    from atlas.pipeline import Step, Runner, Ledger, FatalError

    step = Step(
        name="code.parse",
        units=lambda ctx: [str(p) for p in Path("src").glob("*.adb")],
        fingerprint=lambda ctx, u: sha256_file(u) + cfg.fingerprint("sources.code"),
        run=lambda ctx, u: analyze(u),             # idempotent; writes atomically
        prune=lambda ctx, u: delete_results(u),    # optional: what to do with a vanished unit
        prepare=lambda ctx, units: warm_cache(units),  # optional: once, with the units to process
        estimate=lambda ctx, units: {"requests": {"chat": len(units)}},  # optional: what they will cost
        depends_on=["docs.sections"],
    )
    Runner(ledger=Ledger(workspace / "atlas.db"), steps=[step], context=ctx).run()
    Runner(ledger=Ledger(workspace / "atlas.db"), steps=[step], context=ctx, dry_run=True).run().pending
    Runner(..., deadline=time.time() + 3600, max_requests=1000).run().stopped_by   # "budget" if reached
    Ledger(workspace / "atlas.db").runs()           # the last runs, with their profiles
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from atlas.db import connect

log = logging.getLogger(__name__)


class FatalError(Exception):
    """Error after which it makes no sense to go on with the next unit."""


class Interrupted(BaseException):
    """The run was interrupted (Ctrl+C) while units ran in other threads. A long unit that
    watches `Runner.stopping` raises it to stop early; like a kill, the unit stays
    'running' in the ledger and the next run does it again."""


class WorkspaceLocked(RuntimeError):
    pass


class Stopping(threading.Event):
    """The `stopping` of a Runner: an Event that also sets itself when `check` (the
    Runner's budget: a deadline, a number of requests) says the run must stop. A long unit
    that watches it, such as a document before each section, stops on the budget too,
    without a timer thread: the budget is looked at whenever someone asks."""

    def __init__(self) -> None:
        super().__init__()
        self.check: Callable[[], bool] | None = None

    def is_set(self) -> bool:
        if not super().is_set() and self.check is not None and self.check():
            self.set()
        return super().is_set()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def clock(seconds: float) -> str:
    """'under 1 min', '35 min', '2 h 10 min'."""
    m = round(seconds / 60)
    if m < 1:
        return "under 1 min"
    if m < 90:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min"


def atomic_write(path: str | Path, content: str | bytes) -> None:
    """Atomic write: temporary file + rename. The temporary file is the thread's own, so
    two threads can write the same file at the same time (the last one wins)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    if isinstance(content, str):
        tmp.write_text(content, encoding="utf-8")
    else:
        tmp.write_bytes(content)
    os.replace(tmp, path)


@dataclass
class Step:
    name: str
    units: Callable[[Any], Iterable[str]]
    fingerprint: Callable[[Any, str], str]
    run: Callable[[Any, str], Any]
    prune: Callable[[Any, str], None] | None = None
    depends_on: list[str] = field(default_factory=list)
    is_fatal: Callable[[Exception], bool] = lambda e: isinstance(e, FatalError)
    parallel: bool = True
    workers: int | None = None      # units at the same time; None = those of the Runner
    # Called once with the units about to be processed, before them: to do together what
    # they all need (e.g. their embeddings in full batches). It is only a head start: if
    # it fails, each unit does its own work (a fatal error does stop the run).
    prepare: Callable[[Any, list[str]], None] | None = None
    # Called with the units about to be processed, to say what they will cost before doing
    # anything: {"requests": {"chat": 540}, "seconds": [low, high], "text": "540 chat
    # requests, about 19-22 min at 28/min"}, every key optional. The Runner logs `text`
    # when the step starts and keeps the whole dict in the report (`estimate`); the
    # progress lines use `requests` and `seconds`. If it fails, the step goes on without.
    estimate: Callable[[Any, list[str]], dict] | None = None


class Ledger:
    """State of each (step, unit) in SQLite. One transaction per write."""

    STATUSES = ("ok", "error", "quarantined", "stale", "running")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.db = connect(self.path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS ledger (
                step TEXT NOT NULL, unit TEXT NOT NULL, fingerprint TEXT, status TEXT,
                output_hash TEXT, attempts INTEGER DEFAULT 0, error TEXT,
                started TEXT, finished TEXT, seconds REAL, PRIMARY KEY (step, unit));
            CREATE INDEX IF NOT EXISTS idx_ledger_status ON ledger(step, status);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, started TEXT, seconds REAL, version TEXT, report TEXT);
        """)
        # The connection is shared by the Runner's worker threads, and the sqlite3 module
        # does not serialise them: every read and write goes through this lock. Re-entrant
        # because mark() reads the previous row while holding it.
        self.lock = threading.RLock()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def get(self, step: str, unit: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM ledger WHERE step=? AND unit=?", (step, unit))
        return rows[0] if rows else None

    def units(self, step: str) -> dict[str, sqlite3.Row]:
        return {r["unit"]: r for r in self._query("SELECT * FROM ledger WHERE step=?", (step,))}

    def mark(self, step: str, unit: str, status: str, fingerprint: str | None = None,
             output_hash: str | None = None, error: str | None = None, attempts: int | None = None,
             seconds: float | None = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            prev = self.get(step, unit)
            att = attempts if attempts is not None else (prev["attempts"] if prev else 0)
            self.db.execute(
                "INSERT INTO ledger (step, unit, fingerprint, status, output_hash, attempts, error, started, finished,"
                " seconds) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(step, unit) DO UPDATE SET"
                " fingerprint=excluded.fingerprint, status=excluded.status, output_hash=excluded.output_hash,"
                " attempts=excluded.attempts, error=excluded.error, finished=excluded.finished,"
                " seconds=excluded.seconds",
                (step, unit, fingerprint if fingerprint is not None else (prev["fingerprint"] if prev else None),
                 status, output_hash, att, (error or "")[:500], prev["started"] if prev else now, now,
                 None if seconds is None else round(seconds, 3)))
            self.db.commit()

    def delete(self, step: str, unit: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM ledger WHERE step=? AND unit=?", (step, unit))
            self.db.commit()

    def summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self._query("SELECT step, status, COUNT(*) n FROM ledger GROUP BY step, status"):
            out.setdefault(r["step"], {})[r["status"]] = r["n"]
        return out

    def pending(self) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT step, unit, status, attempts, error FROM ledger WHERE status IN ('error','quarantined','running')"
            " ORDER BY step, unit")]

    def start_run(self, started: str, version: str = "") -> int:
        """The row of a run that starts (without seconds nor report until save_run): its id
        goes in the lines of the log of the run."""
        with self.lock:
            cur = self.db.execute("INSERT INTO runs (started, version, report) VALUES (?,?,?)",
                                  (started, version, "{}"))
            self.db.commit()
            return cur.lastrowid

    def save_run(self, report: "RunReport", version: str = "", run_id: int | None = None) -> None:
        """The report of a run: in its row of start_run, or in a new one."""
        values = (report.started, round(report.seconds, 2), version, json.dumps(report.as_dict(), ensure_ascii=False))
        with self.lock:
            if run_id is None:
                self.db.execute("INSERT INTO runs (started, seconds, version, report) VALUES (?,?,?,?)", values)
            else:
                self.db.execute("UPDATE runs SET started=?, seconds=?, version=?, report=? WHERE id=?",
                                (*values, run_id))
            self.db.commit()

    def runs(self, limit: int = 10) -> list[dict]:
        """The last finished runs, most recent first: id, started, seconds, version and report
        (with the profile of each step). One that is running, or was killed, is left out."""
        return [{**dict(r), "report": json.loads(r["report"])} for r in self._query(
            "SELECT id, started, seconds, version, report FROM runs WHERE seconds IS NOT NULL ORDER BY id DESC "
            "LIMIT ?", (limit,))]


class _WorkspaceLock:
    """One process per workspace. The lock is the operating system's on the lock file
    (flock on POSIX, msvcrt.locking on Windows), not the file itself: the system releases
    it when the process ends, however it ends (kill -9, a crash, a power cut), so a killed
    run never leaves the workspace blocked. The file stays, with the PID of the process
    that holds the lock, only for the error message."""

    WINDOWS_OFFSET = 1 << 20     # Windows locks a byte range: one byte far from the PID

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def _acquire(self, fd: int) -> bool:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, self.WINDOWS_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def __enter__(self) -> "_WorkspaceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        if not self._acquire(fd):
            os.close(fd)
            try:
                pid = self.path.read_text(encoding="ascii", errors="replace").strip() or "?"
            except OSError:
                pid = "?"
            raise WorkspaceLocked(f"another process (PID {pid}) is running this workspace ({self.path})")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return self

    def __exit__(self, *exc) -> None:
        # The file is not deleted: a process waiting on it could otherwise end up holding
        # the lock of a deleted file while another one creates a new file and locks it.
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, self.WINDOWS_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)                 # on POSIX this releases the flock


@dataclass
class RunReport:
    processed: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    quarantined: dict[str, int] = field(default_factory=dict)
    pruned: dict[str, int] = field(default_factory=dict)
    pending: dict[str, int] = field(default_factory=dict)       # units to process, by step
    estimate: dict[str, dict] = field(default_factory=dict)     # by step: what Step.estimate said
    fatal: str | None = None
    stopped_by: str | None = None       # "budget": the deadline or max_requests of the Runner
    seconds: float = 0.0
    started: str = ""
    profile: dict[str, dict] = field(default_factory=dict)      # by step: see Runner._run_step

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


SLOWEST = 3     # units listed in the profile of each step
REQUEST_METERS = ("chat", "embeddings", "other")    # the meters that count requests, for max_requests


class _RunLog(logging.LoggerAdapter):
    """The lines of a run, with the id of its row of the runs table: "run 12: ..."."""

    def process(self, msg, kwargs):
        return (f"run {self.extra['run']}: {msg}" if self.extra.get("run") else msg), kwargs


class Runner:
    """Runs the steps in dependency order, the units of a parallel step in `workers`
    threads (or the step's own `workers`). `meters` returns cumulative counters (requests
    to the APIs, seconds waiting...): the profile of each step keeps what grew during it.
    `version` is saved with each run, to compare profiles between versions. `on_step` is
    called with the name of each step before it starts (atlas.cli labels the usage with it).

    `stopping` is set when the run is interrupted (Ctrl+C) with units running in other
    threads: the units not started yet are cancelled, and the long ones can watch it and
    raise Interrupted so as not to hold the end of the run.

    A budget stops the run cleanly: `deadline` (time.time() after which no unit starts) and
    `max_requests` (what the `meters` in REQUEST_METERS may grow during the run; 0 = no
    limit). They are looked at before each unit and, through `stopping` (a Stopping), by
    the long units that watch it; there is no timer thread. When one is reached, the units
    not started are left for the next run, the ones that stop halfway (Interrupted) stay
    'running' in the ledger as after a kill and are done again next time, the next steps do
    not start and the report says `stopped_by = "budget"`.

    `progress` receives a line every PROGRESS_SECONDS or PROGRESS_UNITS finished units of a
    step: units done, time spent and, with the estimate of the step, the time left
    (log.info by default).

    `dry_run=True` only looks: it finds the units and fingerprints of each step as a run
    would and asks each step's `estimate` what its pending units will cost, without the
    workspace lock (it can look while another process runs), without running or pruning
    anything and without saving the run. The report says `pending` and `estimate` by step;
    a step whose dependencies have pending units (`after` in its estimate) will have more
    to do once they are done, which a dry run cannot see."""

    def __init__(self, ledger: Ledger, steps: list[Step], context: Any = None, workers: int = 1,
                 on_error: str = "continue", max_attempts: int = 3, retry_quarantined: bool = False,
                 prune_missing: bool = True, lock_path: str | Path | None = None,
                 meters: Callable[[], dict[str, float]] | None = None, version: str = "",
                 stopping: threading.Event | None = None, dry_run: bool = False,
                 deadline: float | None = None, max_requests: int = 0,
                 progress: Callable[[str], None] | None = None, on_step: Callable[[str], None] | None = None):
        self.ledger, self.context = ledger, context
        self.steps = self._sort(steps)
        self.workers = max(1, workers)
        self.on_error, self.max_attempts = on_error, max_attempts
        self.retry_quarantined, self.prune_missing = retry_quarantined, prune_missing
        self.lock_path = Path(lock_path) if lock_path else ledger.path.with_suffix(".lock")
        self.meters, self.version = meters, version
        self.stopping = stopping if stopping is not None else Stopping()
        self.dry_run = dry_run
        self.deadline, self.max_requests = deadline, max(0, max_requests)
        self.progress = progress or log.info
        self.budget_hit = ""            # why the budget stopped the run, once it has
        self._run_meter: dict[str, float] = {}
        self.on_step = on_step
        self.run_id: int | None = None
        self.log = _RunLog(log, {"run": None})

    PROGRESS_SECONDS, PROGRESS_UNITS = 30.0, 25

    @staticmethod
    def _sort(steps: list[Step]) -> list[Step]:
        """Topological order by depends_on (stable with respect to the given order)."""
        by_name = {s.name: s for s in steps}
        done, order = set(), []

        def visit(s: Step, stack: tuple = ()):
            if s.name in done:
                return
            if s.name in stack:
                raise ValueError(f"circular dependency between steps: {' -> '.join(stack + (s.name,))}")
            for dep in s.depends_on:
                if dep in by_name:
                    visit(by_name[dep], stack + (s.name,))
            done.add(s.name)
            order.append(s)

        for s in steps:
            visit(s)
        return order

    def run(self, only: Iterable[str] | None = None) -> RunReport:
        rep, t0 = RunReport(started=datetime.now().isoformat(timespec="seconds")), time.perf_counter()
        if self.dry_run:
            for step in self.steps:
                if not only or step.name in set(only):
                    self._look(step, rep)
            rep.seconds = time.perf_counter() - t0
            return rep
        self.stopping.clear()
        self.budget_hit, self._run_meter = "", self._meter()
        if isinstance(self.stopping, Stopping):
            self.stopping.check = self._over_budget
        try:
            with self._lock():
                self.run_id = self.ledger.start_run(rep.started, self.version)
                self.log.extra["run"] = self.run_id
                self.log.info("started, version %s, %d steps: %s", self.version or "?", len(self.steps),
                              ", ".join(s.name for s in self.steps))
                for step in self.steps:
                    if only and step.name not in set(only):
                        continue
                    if self._over_budget():
                        break
                    if self.on_step:
                        self.on_step(step.name)
                    try:
                        self._run_step(step, rep)
                    except FatalError as e:
                        rep.fatal = f"{step.name}: {e}"
                        self.log.error("stopped at %s: %s (the state is saved)", step.name, e)
                        break
                    if self.budget_hit:
                        break
                if self.budget_hit:
                    rep.stopped_by = "budget"
                    self.log.warning("stopped by the budget (%s) at %s; the state is saved", self.budget_hit,
                                     step.name)
                rep.seconds = time.perf_counter() - t0
                self.ledger.save_run(rep, self.version, self.run_id)
                self.log.info("finished in %.1f s: processed %s, errors %s, quarantined %s", rep.seconds,
                              rep.processed or 0, rep.errors or 0, rep.quarantined or 0)
        finally:
            if isinstance(self.stopping, Stopping):
                self.stopping.check = None
        return rep

    def _over_budget(self) -> bool:
        """Whether the deadline has passed or the requests reached max_requests; the first
        time, it says why (budget_hit) and sets `stopping`."""
        if self.budget_hit:
            return True
        reason = ""
        if self.deadline is not None and time.time() >= self.deadline:
            reason = "time limit reached"
        elif self.max_requests:
            now = self._meter()
            used = sum(now.get(k, 0) - self._run_meter.get(k, 0) for k in REQUEST_METERS)
            if used >= self.max_requests:
                reason = f"{used:g} requests, max_requests = {self.max_requests}"
        if reason:
            self.budget_hit = reason
            self.stopping.set()
        return bool(reason)

    def _lock(self) -> "_WorkspaceLock":
        return _WorkspaceLock(self.lock_path)

    def _meter(self) -> dict[str, float]:
        return dict(self.meters()) if self.meters else {}

    def _run_step(self, step: Step, rep: RunReport) -> None:
        """Processes the pending units of a step and leaves its profile in the report: wall
        seconds (`seconds`), of which `scan_seconds` went in finding the units and their
        fingerprints and `prepare_seconds` in Step.prepare; the units (all, and those that
        ran, well or not); the seconds of the units that ran (total, median, maximum) and
        the slowest ones; and what the meters grew (`api`). Also when the step stops on a
        fatal error."""
        start, before = time.perf_counter(), self._meter()
        workers = max(1, step.workers or self.workers) if step.parallel else 1
        prof: dict[str, Any] = {"units": 0, "workers": workers}
        durations: list[tuple[str, float]] = []
        try:
            self._process(step, rep, prof, durations, workers, start)
        finally:
            elapsed = time.perf_counter() - start
            scan = prof.pop("scan_seconds", elapsed)          # not there if finding the units failed
            prof = {"seconds": round(elapsed, 3), "scan_seconds": round(scan, 3), **prof, "ran": len(durations)}
            if "prepare_seconds" in prof:
                prof["prepare_seconds"] = round(prof["prepare_seconds"], 3)
            if durations:
                times = sorted(s for _, s in durations)
                prof["unit_seconds"] = {"total": round(sum(times), 3), "median": round(times[len(times) // 2], 3),
                                        "max": round(times[-1], 3)}
                prof["slowest"] = [{"unit": u, "seconds": round(s, 3)}
                                   for u, s in sorted(durations, key=lambda d: -d[1])[:SLOWEST]]
            after = self._meter()
            grown = {k: round(v - before.get(k, 0), 3) for k, v in after.items() if v != before.get(k, 0)}
            if grown:
                prof["api"] = grown
            rep.profile[step.name] = prof

    def _scan(self, step: Step, rep: RunReport) -> tuple[list[str], dict, list[tuple[str, str]]]:
        """The units of a step, their ledger rows and the (unit, fingerprint) to process."""
        units = list(step.units(self.context))
        previous = self.ledger.units(step.name)
        pending: list[tuple[str, str]] = []
        for u in units:
            fp = step.fingerprint(self.context, u)
            prev = previous.get(u)
            if prev and prev["status"] == "ok" and prev["fingerprint"] == fp:
                rep.skipped[step.name] = rep.skipped.get(step.name, 0) + 1
                continue
            if prev and prev["status"] == "quarantined" and prev["fingerprint"] == fp and not self.retry_quarantined:
                rep.quarantined[step.name] = rep.quarantined.get(step.name, 0) + 1
                continue
            pending.append((u, fp))
        rep.pending[step.name] = len(pending)
        return units, previous, pending

    def _estimate(self, step: Step, rep: RunReport, pending: list[str]) -> dict:
        """What Step.estimate says of the pending units, in the report; {} without it, with
        nothing pending or if it fails. `after`: the dependencies with units still to
        process in a dry run (their results are not there yet)."""
        est: dict = {}
        if step.estimate and pending:
            try:
                est = dict(step.estimate(self.context, pending) or {})
            except Exception as e:
                log.warning("%s: no estimate: %s", step.name, e)
        if self.dry_run:
            after = [d for d in step.depends_on if rep.pending.get(d)]
            if after:
                est["after"] = after
        if est:
            rep.estimate[step.name] = est
        return est

    def _look(self, step: Step, rep: RunReport) -> None:
        """A dry run of a step: its pending units and their estimate, nothing else."""
        _, _, pending = self._scan(step, rep)
        est = self._estimate(step, rep, [u for u, _ in pending])
        log.debug("%s: %d to process, %d up to date%s", step.name, len(pending), rep.skipped.get(step.name, 0),
                 f"; {est['text']}" if est.get("text") else "")

    def _process(self, step: Step, rep: RunReport, prof: dict, durations: list[tuple[str, float]],
                 workers: int, start: float) -> None:
        units, previous, pending = self._scan(step, rep)

        # Vanished units: stale and, if requested, pruned
        current = set(units)
        for u, prev in previous.items():
            if u not in current and prev["status"] != "stale":
                if self.prune_missing:
                    if step.prune:
                        step.prune(self.context, u)
                    self.ledger.delete(step.name, u)
                else:
                    self.ledger.mark(step.name, u, "stale")
                rep.pruned[step.name] = rep.pruned.get(step.name, 0) + 1
        prof.update(units=len(units), scan_seconds=time.perf_counter() - start)

        self.log.info("%s: %d units, %d to process, %d up to date", step.name, len(units),
                 len(pending), rep.skipped.get(step.name, 0))
        if not pending or self._over_budget():
            return
        t0 = time.perf_counter()
        est = self._estimate(step, rep, [u for u, _ in pending])
        if est:
            prof["estimate_seconds"] = round(time.perf_counter() - t0, 3)
        if est.get("text"):
            log.info("%s: %s", step.name, est["text"])
        began, meter_before = time.perf_counter(), self._meter()
        if step.prepare:
            t0 = time.perf_counter()
            try:
                step.prepare(self.context, [u for u, _ in pending])
            except Exception as e:
                if isinstance(e, FatalError):
                    raise
                if step.is_fatal(e):
                    raise FatalError(str(e)) from e
                self.log.warning("%s: the preparation failed, each unit does its own work: %s", step.name, e)
            prof["prepare_seconds"] = time.perf_counter() - t0

        fatal: list[Exception] = []
        result_lock = threading.Lock()
        shown = {"done": 0, "at": began}

        def finished() -> None:
            """One more unit done (with result_lock held): the progress line, when due."""
            shown["done"] += 1
            done, now = shown["done"], time.perf_counter()
            if done >= len(pending) or (done % self.PROGRESS_UNITS and now - shown["at"] < self.PROGRESS_SECONDS):
                return
            shown["at"], elapsed = now, now - began
            expected = est.get("requests") or {}
            grown = self._meter()
            made = sum(grown.get(k, 0) - meter_before.get(k, 0) for k in expected)
            total = sum(expected.values())
            # by the requests the estimate expects, if they are being made; if not, by the units
            left = elapsed * (total - made) / made if 0 < made < total else elapsed * (len(pending) - done) / done
            left_text = clock(left) if clock(left).startswith("under") else f"~{clock(left)}"
            self.progress(f"{step.name} {done}/{len(pending)} units, {clock(elapsed)}, {left_text} left")

        def process(u: str, fp: str) -> None:
            if fatal or self._over_budget() or self.stopping.is_set():
                return
            prev = self.ledger.get(step.name, u)
            attempts = (prev["attempts"] if prev and prev["fingerprint"] == fp else 0)
            # the fingerprint is saved on start: if the process dies, the unit stays 'running'
            self.ledger.mark(step.name, u, "running", fingerprint=fp, attempts=attempts)
            t0 = time.perf_counter()
            try:
                output = step.run(self.context, u)
                took = time.perf_counter() - t0
                self.ledger.mark(step.name, u, "ok", fingerprint=fp,
                                 output_hash=sha256_text(json.dumps(output, sort_keys=True, default=str))[:16]
                                 if output is not None else None, attempts=attempts, seconds=took)
                with result_lock:
                    rep.processed[step.name] = rep.processed.get(step.name, 0) + 1
                    durations.append((u, took))
                    finished()
            except Interrupted:
                if not self.budget_hit:
                    raise
                # stopped halfway by the budget: 'running' in the ledger, like after a kill
                with result_lock:
                    prof["interrupted"] = prof.get("interrupted", 0) + 1
                log.info("%s %s: stopped by the budget; it is done again on the next run", step.name, u)
            except Exception as e:
                took = time.perf_counter() - t0
                with result_lock:
                    durations.append((u, took))
                if step.is_fatal(e):
                    self.ledger.mark(step.name, u, "error", fingerprint=fp, error=f"fatal: {e}", attempts=attempts,
                                     seconds=took)
                    fatal.append(e)
                    return
                attempts += 1
                status = "quarantined" if attempts >= self.max_attempts else "error"
                self.ledger.mark(step.name, u, status, fingerprint=fp,
                                 error=f"{type(e).__name__}: {e}", attempts=attempts, seconds=took)
                with result_lock:
                    key = "quarantined" if status == "quarantined" else "errors"
                    getattr(rep, key)[step.name] = getattr(rep, key).get(step.name, 0) + 1
                    finished()
                self.log.warning("%s %s: %s", step.name, u, e)
                if self.on_error == "stop":
                    fatal.append(FatalError(f"on_error = stop after {u} failed: {e}"))

        if workers > 1:
            pool = ThreadPoolExecutor(max_workers=workers)
            try:
                for f in as_completed([pool.submit(process, u, fp) for u, fp in pending]):
                    f.result()
            except BaseException:
                # Ctrl+C (only the main thread gets it): the units not started yet are
                # cancelled, and the running ones are told to stop and waited for, so
                # that none is still writing when the workspace lock is released
                self.stopping.set()
                pool.shutdown(wait=True, cancel_futures=True)
                raise
            pool.shutdown()
        else:
            for u, fp in pending:
                process(u, fp)
                if fatal:
                    break
        if fatal:
            e = fatal[0]
            raise e if isinstance(e, FatalError) else FatalError(str(e))
