import pytest
from atlas.pipeline import FatalError, Ledger, Runner, Step


def _step(name, units, fingerprints, run, **kw):
    return Step(name=name, units=lambda ctx: list(units), fingerprint=lambda ctx, u: fingerprints[u],
                run=run, **kw)


def test_incremental_skips_done_work_and_reprocesses_changes(tmp_path):
    done = []
    fingerprints = {"a": "1", "b": "1"}
    step = _step("p", ["a", "b"], fingerprints, lambda ctx, u: done.append(u) or {"u": u})
    r = Runner(Ledger(tmp_path / "db"), [step], workers=1)
    assert r.run().processed == {"p": 2}
    rep = r.run()
    assert rep.skipped == {"p": 2} and rep.processed == {}
    fingerprints["b"] = "2"                             # one unit changes
    rep = r.run()
    assert rep.processed == {"p": 1} and done == ["a", "b", "b"]


def test_resumes_after_an_interruption_midway(tmp_path):
    done, crash = [], {"on": True}

    def run(ctx, u):
        if u == "b" and crash["on"]:
            raise KeyboardInterrupt            # simulates Ctrl+C / kill in the middle of unit b
        done.append(u)

    step = _step("p", ["a", "b", "c"], {"a": "1", "b": "1", "c": "1"}, run, parallel=False)
    ledger = Ledger(tmp_path / "db")
    with pytest.raises(KeyboardInterrupt):
        Runner(ledger, [step], workers=1).run()
    assert ledger.get("p", "a")["status"] == "ok" and ledger.get("p", "b")["status"] == "running"
    assert (tmp_path / "db").with_suffix(".lock").read_text() == ""   # the lock is released
    crash["on"] = False
    rep = Runner(ledger, [step], workers=1).run()
    assert rep.processed == {"p": 2} and rep.skipped == {"p": 1} and done == ["a", "b", "c"]


def test_errors_quarantine_and_fatal(tmp_path):
    def run(ctx, u):
        if u == "bad":
            raise ValueError("cannot do it")
        if u == "fatal":
            raise FatalError("no connection")
        return 1

    ledger = Ledger(tmp_path / "db")
    step = _step("p", ["ok", "bad"], {"ok": "1", "bad": "1"}, run)
    r = Runner(ledger, [step], max_attempts=2)
    assert r.run().errors == {"p": 1}
    assert r.run().quarantined == {"p": 1}                # second failure: quarantine
    assert r.run().quarantined == {"p": 1} and ledger.get("p", "bad")["status"] == "quarantined"
    assert Runner(ledger, [step], max_attempts=2, retry_quarantined=True).run().quarantined == {"p": 1}
    step2 = _step("q", ["fatal", "after"], {"fatal": "1", "after": "1"}, run, parallel=False)
    rep = Runner(ledger, [step2]).run()
    assert rep.fatal and ledger.get("q", "after") is None         # never touched


def test_prunes_vanished_units(tmp_path):
    pruned = []
    units = ["a", "b"]
    step = Step(name="p", units=lambda ctx: list(units), fingerprint=lambda ctx, u: "1",
                run=lambda ctx, u: 1, prune=lambda ctx, u: pruned.append(u))
    ledger = Ledger(tmp_path / "db")
    Runner(ledger, [step]).run()
    units.remove("a")
    rep = Runner(ledger, [step]).run()
    assert rep.pruned == {"p": 1} and pruned == ["a"] and ledger.get("p", "a") is None


def test_dependency_order_and_cycles(tmp_path):
    order = []
    steps = [_step("c", ["x"], {"x": "1"}, lambda ctx, u: order.append("c"), depends_on=["b"]),
             _step("a", ["x"], {"x": "1"}, lambda ctx, u: order.append("a")),
             _step("b", ["x"], {"x": "1"}, lambda ctx, u: order.append("b"), depends_on=["a"])]
    Runner(Ledger(tmp_path / "db"), steps).run()
    assert order == ["a", "b", "c"]
    with pytest.raises(ValueError, match="circular"):
        Runner(Ledger(tmp_path / "db2"), [_step("a", [], {}, None, depends_on=["b"]),
                                           _step("b", [], {}, None, depends_on=["a"])])


def test_parallel_units_share_the_ledger_and_store_safely(tmp_path):
    from atlas.plugins import CodeUnit
    from atlas.store import Store
    store = Store(tmp_path / "ws")
    ledger = Ledger(store.path)                 # same atlas.db as the store, like the CLI
    units = [f"u{i}" for i in range(300)]

    def run(ctx, u):
        store.put_code_unit(CodeUnit(file=u, unit=u, language="ada"))
        return store.counts()

    rep = Runner(ledger, [_step("p", units, {u: "1" for u in units}, run)], workers=8).run()
    assert rep.processed == {"p": 300} and not rep.errors
    assert ledger.summary() == {"p": {"ok": 300}} and store.counts()["code_units"] == 300


def test_kill_9_releases_the_workspace_and_the_next_run_resumes(tmp_path):
    """A run killed in the middle of a unit (kill -9: no cleanup at all) leaves no lock
    behind: the next run starts, redoes that unit and does not repeat the finished ones."""
    import subprocess
    import sys
    import time
    from pathlib import Path
    from atlas.pipeline import WorkspaceLocked
    root, db, started = Path(__file__).resolve().parents[1], tmp_path / "atlas.db", tmp_path / "started_b"
    child = tmp_path / "slow_run.py"
    child.write_text(f"""
import sys, time
sys.path.insert(0, {str(root)!r})
from pathlib import Path
from atlas.pipeline import Ledger, Runner, Step

def run(ctx, u):
    if u == "b":
        Path({str(started)!r}).touch()
        time.sleep(60)                      # killed here

Runner(Ledger({str(db)!r}), [Step(name="s", units=lambda ctx: ["a", "b", "c"],
                                  fingerprint=lambda ctx, u: "1", run=run)]).run()
""")
    proc = subprocess.Popen([sys.executable, str(child)])
    try:
        for _ in range(100):
            if started.exists():
                break
            time.sleep(0.1)
        assert started.exists()
        done = []
        step = _step("s", ["a", "b", "c"], {u: "1" for u in "abc"}, lambda ctx, u: done.append(u))
        with pytest.raises(WorkspaceLocked, match=f"PID {proc.pid}"):   # still running: refused
            Runner(Ledger(db), [step]).run()
    finally:
        proc.kill()                          # SIGKILL (TerminateProcess on Windows)
        proc.wait()
    rep = Runner(Ledger(db), [step]).run()
    assert rep.processed == {"s": 2} and rep.skipped == {"s": 1} and done == ["b", "c"]


def test_a_lock_file_left_by_an_old_version_does_not_block(tmp_path):
    (tmp_path / "atlas.lock").write_text("12345")          # O_EXCL lock of a killed run
    step = _step("s", ["a"], {"a": "1"}, lambda ctx, u: None)
    assert Runner(Ledger(tmp_path / "atlas.db"), [step]).run().processed == {"s": 1}
