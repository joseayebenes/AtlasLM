"""atlas.logs: the log of every run, on stderr and in a file per day of the workspace."""
import logging
from datetime import date, timedelta

import pytest

from atlas.config import ConfigError, load_config
from atlas.logs import log_file, remove_old_logs, setup_logging


def test_a_file_per_day_with_atlas_at_debug_and_the_old_ones_deleted(toml, tmp_path):
    cfg = load_config(toml())
    folder = cfg.workspace / "logs"
    folder.mkdir(parents=True)
    old, recent = folder / f"atlas-{date.today() - timedelta(days=20)}.log", \
        folder / f"atlas-{date.today() - timedelta(days=3)}.log"
    for f in (old, recent, folder / "notes.txt"):
        f.write_text("x\n")
    from atlas.llm import set_usage_label
    set_usage_label("status")                         # what atlas.cli does when it starts
    path = setup_logging(cfg, "status")
    set_usage_label("")
    assert path == log_file(cfg) == folder / f"atlas-{date.today()}.log"
    assert not old.exists() and recent.exists() and (folder / "notes.txt").exists()     # log_keep_days = 14
    set_usage_label("status")
    logging.getLogger("atlas.graph").debug("a detail of atlas")
    logging.getLogger("openai").debug("a detail of a library")
    logging.getLogger("openai").warning("a warning of a library")
    set_usage_label("")
    for h in logging.getLogger().handlers:
        h.flush()
    text = path.read_text(encoding="utf-8")
    assert "atlas.graph: a detail of atlas" in text and "a warning of a library" in text
    assert "a detail of a library" not in text
    assert "DEBUG" in text and "[status]" in text and f"with {cfg.path}" in text       # the first line
    assert setup_logging(cfg, "status") == path and text.count(f"with {cfg.path}") == 1   # once per action

    assert remove_old_logs(folder, 0) == []                  # 0: every file stays
    assert remove_old_logs(folder, 2) == [recent]


def _with_project(toml, line: str):
    path = toml()
    path.write_text(path.read_text().replace("[project]\n", f"[project]\n{line}\n"))
    return path


def test_log_dir_empty_turns_the_file_off_and_keep_days_is_checked(toml, capsys):
    from atlas.cli import status
    cfg = load_config(_with_project(toml, 'log_dir = ""'))
    assert setup_logging(cfg, "status") is None and log_file(cfg) is None
    assert status(cfg) == 0
    assert "log: off ([project] log_dir is empty)" in capsys.readouterr().out
    assert not (cfg.workspace / "logs").exists()
    with pytest.raises(ConfigError, match="log_keep_days"):
        load_config(_with_project(toml, "log_keep_days = -1"))


def test_a_run_leaves_its_lines_with_the_id_of_its_row_and_the_end_of_the_action(toml, capsys):
    import json
    from atlas.cli import main
    from atlas.pipeline import Ledger
    path = toml('[pipeline]\nsteps = ["doors"]\n')
    for _ in range(2):
        with pytest.raises(SystemExit) as e:
            main(["run", str(path)])
        assert e.value.code == 0
    capsys.readouterr()
    cfg = load_config(path)
    runs = Ledger(cfg.workspace / "atlas.db").runs()
    assert [r["id"] for r in runs] == [2, 1] and runs[0]["report"]["processed"] == {}
    text = log_file(cfg).read_text(encoding="utf-8")
    assert "run 1: started, version" in text and "run 2: doors.requirements: 3 units, 0 to process" in text
    assert "[run doors.requirements]" in text and "end of run: exit code 0" in text
    with pytest.raises(SystemExit):
        main(["status", str(path)])
    assert f"log: {log_file(cfg)}" in capsys.readouterr().out


def test_the_notices_of_the_limiter_are_warnings_of_atlas_llm(caplog):
    from atlas.llm.connection import _log
    with caplog.at_level(logging.WARNING, logger="atlas.llm"):
        _log("[API gateway] HTTP 429 (rate limit): waiting 5s")
    assert [(r.name, r.levelname) for r in caplog.records] == [("atlas.llm", "WARNING")]
