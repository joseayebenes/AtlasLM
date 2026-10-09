"""Motor de procesamiento incremental y recuperable.

Un `Step` declara sus unidades, cómo calcular la huella de cada una y cómo procesarla.
El `Runner` consulta el `Ledger` (SQLite): si la huella no ha cambiado y la unidad está
ok, la omite; si no, la procesa. Los errores propios de una unidad se anotan y se sigue;
los fatales (conexión, credenciales, cupo agotado) paran la ejecución dejando el estado
guardado. Volver a ejecutar reanuda. Las unidades que desaparecen se marcan obsoletas y,
opcionalmente, se podan.

    from atlas.pipeline import Step, Runner, Ledger, FatalError

    paso = Step(
        name="code.parse",
        units=lambda ctx: [str(p) for p in Path("src").glob("*.adb")],
        fingerprint=lambda ctx, u: sha256_file(u) + cfg.fingerprint("sources.code"),
        run=lambda ctx, u: analizar(u),            # idempotente; escribe de forma atómica
        prune=lambda ctx, u: borrar_resultados(u), # opcional: qué hacer con una unidad desaparecida
        depends_on=["docs.sections"],
    )
    Runner(ledger=Ledger(workspace / "atlas.db"), steps=[paso], context=ctx).run()
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

log = logging.getLogger(__name__)


class FatalError(Exception):
    """Error tras el que no tiene sentido seguir con la siguiente unidad."""


class WorkspaceLocked(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def sha256_text(*partes: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in partes).encode("utf-8")).hexdigest()


def atomic_write(path: str | Path, content: str | bytes) -> None:
    """Escritura atómica: fichero temporal + renombrado."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
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


class Ledger:
    """Estado de cada (paso, unidad) en SQLite. Una transacción por escritura."""

    ESTADOS = ("ok", "error", "cuarentena", "obsoleta", "en_curso")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS ledger (
                step TEXT NOT NULL, unit TEXT NOT NULL, fingerprint TEXT, status TEXT,
                output_hash TEXT, attempts INTEGER DEFAULT 0, error TEXT,
                started TEXT, finished TEXT, PRIMARY KEY (step, unit));
            CREATE INDEX IF NOT EXISTS idx_ledger_status ON ledger(step, status);
        """)
        self.lock = threading.Lock()

    def get(self, step: str, unit: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM ledger WHERE step=? AND unit=?", (step, unit)).fetchone()

    def units(self, step: str) -> dict[str, sqlite3.Row]:
        return {r["unit"]: r for r in self.db.execute("SELECT * FROM ledger WHERE step=?", (step,))}

    def mark(self, step: str, unit: str, status: str, fingerprint: str | None = None,
             output_hash: str | None = None, error: str | None = None, attempts: int | None = None) -> None:
        ahora = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            prev = self.get(step, unit)
            att = attempts if attempts is not None else (prev["attempts"] if prev else 0)
            self.db.execute(
                "INSERT INTO ledger (step, unit, fingerprint, status, output_hash, attempts, error, started, finished)"
                " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(step, unit) DO UPDATE SET fingerprint=excluded.fingerprint,"
                " status=excluded.status, output_hash=excluded.output_hash, attempts=excluded.attempts,"
                " error=excluded.error, finished=excluded.finished",
                (step, unit, fingerprint if fingerprint is not None else (prev["fingerprint"] if prev else None),
                 status, output_hash, att, (error or "")[:500], prev["started"] if prev else ahora, ahora))
            self.db.commit()

    def delete(self, step: str, unit: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM ledger WHERE step=? AND unit=?", (step, unit))
            self.db.commit()

    def summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.db.execute("SELECT step, status, COUNT(*) n FROM ledger GROUP BY step, status"):
            out.setdefault(r["step"], {})[r["status"]] = r["n"]
        return out

    def pending(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT step, unit, status, attempts, error FROM ledger WHERE status IN ('error','cuarentena','en_curso')"
            " ORDER BY step, unit")]


@dataclass
class RunReport:
    processed: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    quarantined: dict[str, int] = field(default_factory=dict)
    pruned: dict[str, int] = field(default_factory=dict)
    fatal: str | None = None
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class Runner:
    def __init__(self, ledger: Ledger, steps: list[Step], context: Any = None, workers: int = 1,
                 on_error: str = "continue", max_attempts: int = 3, retry_quarantined: bool = False,
                 prune_missing: bool = True, lock_path: str | Path | None = None):
        self.ledger, self.context = ledger, context
        self.steps = self._ordenar(steps)
        self.workers = max(1, workers)
        self.on_error, self.max_attempts = on_error, max_attempts
        self.retry_quarantined, self.prune_missing = retry_quarantined, prune_missing
        self.lock_path = Path(lock_path) if lock_path else ledger.path.with_suffix(".lock")

    @staticmethod
    def _ordenar(steps: list[Step]) -> list[Step]:
        """Orden topológico por depends_on (estable respecto al orden dado)."""
        por_nombre = {s.name: s for s in steps}
        hechos, orden = set(), []

        def visitar(s: Step, pila: tuple = ()):
            if s.name in hechos:
                return
            if s.name in pila:
                raise ValueError(f"dependencia circular entre pasos: {' -> '.join(pila + (s.name,))}")
            for dep in s.depends_on:
                if dep in por_nombre:
                    visitar(por_nombre[dep], pila + (s.name,))
            hechos.add(s.name)
            orden.append(s)

        for s in steps:
            visitar(s)
        return orden

    def run(self, only: Iterable[str] | None = None) -> RunReport:
        rep, t0 = RunReport(), time.time()
        with self._lock():
            for step in self.steps:
                if only and step.name not in set(only):
                    continue
                try:
                    self._run_step(step, rep)
                except FatalError as e:
                    rep.fatal = f"{step.name}: {e}"
                    log.error("ejecución detenida en %s: %s (el estado queda guardado)", step.name, e)
                    break
        rep.seconds = time.time() - t0
        return rep

    def _lock(self):
        runner = self

        class _Lock:
            def __enter__(self):
                try:
                    fd = os.open(runner.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode())
                    os.close(fd)
                except FileExistsError:
                    raise WorkspaceLocked(f"otro proceso está ejecutando este workspace ({runner.lock_path});"
                                          f" si no es así, borra el fichero de bloqueo") from None
                return self

            def __exit__(self, *a):
                try:
                    os.remove(runner.lock_path)
                except FileNotFoundError:
                    pass

        return _Lock()

    def _run_step(self, step: Step, rep: RunReport) -> None:
        unidades = list(step.units(self.context))
        previas = self.ledger.units(step.name)
        pendientes: list[tuple[str, str]] = []
        for u in unidades:
            fp = step.fingerprint(self.context, u)
            prev = previas.get(u)
            if prev and prev["status"] == "ok" and prev["fingerprint"] == fp:
                rep.skipped[step.name] = rep.skipped.get(step.name, 0) + 1
                continue
            if prev and prev["status"] == "cuarentena" and prev["fingerprint"] == fp and not self.retry_quarantined:
                rep.quarantined[step.name] = rep.quarantined.get(step.name, 0) + 1
                continue
            pendientes.append((u, fp))

        # Unidades desaparecidas: obsoletas y, si procede, podadas
        actuales = set(unidades)
        for u, prev in previas.items():
            if u not in actuales and prev["status"] != "obsoleta":
                if self.prune_missing:
                    if step.prune:
                        step.prune(self.context, u)
                    self.ledger.delete(step.name, u)
                else:
                    self.ledger.mark(step.name, u, "obsoleta")
                rep.pruned[step.name] = rep.pruned.get(step.name, 0) + 1

        log.info("%s: %d unidades, %d por procesar, %d al día", step.name, len(unidades),
                 len(pendientes), rep.skipped.get(step.name, 0))
        if not pendientes:
            return

        fatal: list[Exception] = []
        resultado_lock = threading.Lock()

        def procesar(u: str, fp: str) -> None:
            if fatal:
                return
            prev = self.ledger.get(step.name, u)
            intentos = (prev["attempts"] if prev and prev["fingerprint"] == fp else 0)
            # la huella se guarda al empezar: si el proceso muere, la unidad queda 'en_curso'
            self.ledger.mark(step.name, u, "en_curso", fingerprint=fp, attempts=intentos)
            try:
                salida = step.run(self.context, u)
                self.ledger.mark(step.name, u, "ok", fingerprint=fp,
                                 output_hash=sha256_text(json.dumps(salida, sort_keys=True, default=str))[:16]
                                 if salida is not None else None, attempts=intentos)
                with resultado_lock:
                    rep.processed[step.name] = rep.processed.get(step.name, 0) + 1
            except Exception as e:
                if step.is_fatal(e):
                    self.ledger.mark(step.name, u, "error", fingerprint=fp, error=f"fatal: {e}", attempts=intentos)
                    fatal.append(e)
                    return
                intentos += 1
                estado = "cuarentena" if intentos >= self.max_attempts else "error"
                self.ledger.mark(step.name, u, estado, fingerprint=fp,
                                 error=f"{type(e).__name__}: {e}", attempts=intentos)
                with resultado_lock:
                    clave = "quarantined" if estado == "cuarentena" else "errors"
                    getattr(rep, clave)[step.name] = getattr(rep, clave).get(step.name, 0) + 1
                log.warning("%s %s: %s", step.name, u, e)
                if self.on_error == "stop":
                    fatal.append(FatalError(f"on_error = stop tras fallar {u}: {e}"))

        if step.parallel and self.workers > 1:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futuros = [pool.submit(procesar, u, fp) for u, fp in pendientes]
                for f in as_completed(futuros):
                    f.result()
        else:
            for u, fp in pendientes:
                procesar(u, fp)
                if fatal:
                    break
        if fatal:
            e = fatal[0]
            raise e if isinstance(e, FatalError) else FatalError(str(e))
