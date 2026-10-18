"""The log of every run: stderr at [project] log_level, and a file per day in the workspace.

    setup_logging(cfg, "run")       # what atlas.cli does before every action
    log_file(cfg)                   # <workspace>/logs/atlas-2026-10-17.log, or None

The file gets everything of atlas at DEBUG and the warnings of the libraries, with the date,
the level, the process and what it was doing ([command step], see atlas.llm.usage_label):
after a night run there is somewhere to read what failed. `atlas run` and `atlas serve` work
over the same workspace at the same time and append to the same file; it is not rotated by
renaming (Windows does not rename a file another process has open): a new file starts each
day, and those older than [project] log_keep_days are deleted when atlas starts (a file
another process still has open is left for the next time). [project] log_dir = "" turns the
file off.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

__all__ = ["DailyFileHandler", "log_dir", "log_file", "remove_old_logs", "setup_logging"]

log = logging.getLogger("atlas")

FILE_FORMAT = "%(asctime)s %(levelname)-7s %(process)d [%(label)s] %(name)s: %(message)s"
STDERR_FORMAT = "%(levelname)s %(name)s: %(message)s"
NAME = re.compile(r"^atlas-(\d{4}-\d{2}-\d{2})\.log$")
QUIET = ("httpx", "httpcore")                 # a line per request otherwise
LIBRARIES_AT_INFO = ("openai", "urllib3", "asyncio", "anyio", "mcp", "google", "google_adk", "pdfminer",
                     "markitdown", "PIL", "matplotlib", "opentelemetry")   # their DEBUG is not wanted


def log_dir(cfg) -> Path | None:
    """The folder of the log files: [project] log_dir, relative to the workspace; None if
    it is empty."""
    folder = cfg["project"].get("log_dir", "logs")
    if not folder:
        return None
    p = Path(folder).expanduser()
    return p if p.is_absolute() else cfg.workspace / p


def log_file(cfg, day: date | None = None) -> Path | None:
    """The log file of a day (today by default), or None if [project] log_dir is empty."""
    folder = log_dir(cfg)
    return folder / f"atlas-{(day or date.today()).isoformat()}.log" if folder else None


class _Label(logging.Filter):
    """Adds what the process is doing (command and step) to each record, and lets through the
    DEBUG lines of atlas only."""

    def filter(self, record: logging.LogRecord) -> bool:
        from atlas.llm.connection import usage_label
        record.label = " ".join(x for x in usage_label() if x) or "-"
        return record.levelno >= logging.INFO or record.name == "atlas" or record.name.startswith("atlas.")


class DailyFileHandler(logging.FileHandler):
    """Appends to <folder>/atlas-<day>.log, and goes on to the file of the next day after
    midnight (atlas serve runs for days). It never renames a file."""

    def __init__(self, folder: Path):
        self.folder, self.day = Path(folder), date.today()
        self.folder.mkdir(parents=True, exist_ok=True)
        super().__init__(self._path(self.day), mode="a", encoding="utf-8", delay=True)

    def _path(self, day: date) -> Path:
        return self.folder / f"atlas-{day.isoformat()}.log"

    def emit(self, record: logging.LogRecord) -> None:
        today = date.today()
        if today != self.day:                 # handle() holds the lock of the handler
            if self.stream:
                self.stream.close()
                self.stream = None
            self.day, self.baseFilename = today, str(self._path(today).resolve())
        super().emit(record)


def remove_old_logs(folder: Path, keep_days: int, today: date | None = None) -> list[Path]:
    """Deletes the log files of `folder` older than `keep_days` (0 = none is deleted), by the
    day in their name. One that cannot be deleted (open in another process on Windows) stays.
    Returns those deleted."""
    if keep_days <= 0 or not folder.is_dir():
        return []
    oldest, gone = (today or date.today()) - timedelta(days=keep_days), []
    for f in folder.iterdir():
        m = NAME.match(f.name)
        try:
            if m and date.fromisoformat(m[1]) < oldest:
                f.unlink()
                gone.append(f)
        except (OSError, ValueError):
            continue
    return gone


_STATE: dict = {"handlers": [], "key": None}


def setup_logging(cfg, action: str = "") -> Path | None:
    """The log of this process for `action` with the configuration `cfg`: a handler of stderr
    at [project] log_level and, unless [project] log_dir is "", one of the file of the day at
    DEBUG; the old files are deleted. Writes a line with the version, the action and the
    configuration. Called again with the same cfg and action, it does nothing; with others, it
    replaces the handlers it put. Returns the log file, or None."""
    from atlas import __version__
    key = (str(cfg.path), action, cfg["project"]["log_level"], str(log_dir(cfg)))
    if key == _STATE["key"]:
        return log_file(cfg)
    root = logging.getLogger()
    for h in _STATE["handlers"]:
        root.removeHandler(h)
        h.close()
    level = logging.getLevelName(str(cfg["project"]["log_level"]).upper())
    level = level if isinstance(level, int) else logging.INFO
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(level)
    stderr.setFormatter(logging.Formatter(STDERR_FORMAT))
    handlers: list[logging.Handler] = [stderr]
    folder, problem = log_dir(cfg), ""
    if folder is not None:
        try:
            remove_old_logs(folder, int(cfg["project"].get("log_keep_days", 14)))
            handler = DailyFileHandler(folder)
            handler.setLevel(logging.DEBUG)
            handler.addFilter(_Label())
            handler.setFormatter(logging.Formatter(FILE_FORMAT))
            handlers.append(handler)
        except OSError as e:                  # a log that cannot be written does not stop atlas
            folder, problem = None, f"the log file cannot be written in {log_dir(cfg)}: {e}"
    for h in handlers:
        root.addHandler(h)
    root.setLevel(logging.DEBUG if folder is not None else level)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in LIBRARIES_AT_INFO:
        if logging.getLogger(name).level == logging.NOTSET:
            logging.getLogger(name).setLevel(logging.INFO)
    _STATE.update(handlers=handlers, key=key)
    if problem:
        log.warning(problem)
    log.debug("atlas %s %s with %s (python %s)", __version__, action or "-", cfg.path, sys.version.split()[0])
    return log_file(cfg) if folder is not None else None
