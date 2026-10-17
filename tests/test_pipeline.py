import pytest
from atlas.pipeline import FatalError, Ledger, Runner, Step, atomic_write


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


def test_a_configuration_error_stops_the_run_instead_of_failing_each_unit(tmp_path):
    from atlas.config import ConfigError

    def run(ctx, u):
        raise ConfigError("CA certificate /missing.pem does not exist")

    ledger = Ledger(tmp_path / "db")
    rep = Runner(ledger, [_step("p", ["a", "b"], {"a": "1", "b": "1"}, run, parallel=False)]).run()
    assert rep.fatal == "p: CA certificate /missing.pem does not exist" and ledger.get("p", "b") is None


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


@pytest.mark.slow
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


def test_each_run_leaves_a_time_profile(tmp_path):
    import time
    meter = {"chat": 0, "wait_seconds": 0.0}

    def run(ctx, u):
        time.sleep(0.05 if u == "slow" else 0)
        meter["chat"] += 1

    step = _step("p", ["fast", "slow"], {"fast": "1", "slow": "1"}, run)
    ledger = Ledger(tmp_path / "db")
    rep = Runner(ledger, [step], workers=2, meters=lambda: dict(meter), version="9.9").run()
    prof = rep.profile["p"]
    assert prof["units"] == 2 and prof["ran"] == 2 and prof["workers"] == 2
    assert prof["slowest"][0]["unit"] == "slow" and prof["unit_seconds"]["max"] >= 0.05
    assert prof["api"] == {"chat": 2} and prof["seconds"] >= prof["scan_seconds"]
    assert ledger.get("p", "slow")["seconds"] >= 0.05 and ledger.get("p", "fast")["seconds"] < 0.05
    again = Runner(ledger, [step], meters=lambda: dict(meter)).run()      # nothing to do
    assert again.profile["p"]["ran"] == 0 and "api" not in again.profile["p"] and "slowest" not in again.profile["p"]
    runs = ledger.runs()
    assert [r["version"] for r in runs] == ["", "9.9"] and runs[1]["report"]["profile"]["p"]["ran"] == 2


def test_the_profile_is_kept_when_a_step_stops(tmp_path):
    def run(ctx, u):
        raise FatalError("no connection")

    ledger = Ledger(tmp_path / "db")
    rep = Runner(ledger, [_step("p", ["a", "b"], {"a": "1", "b": "1"}, run, parallel=False)]).run()
    assert rep.fatal and rep.profile["p"]["ran"] == 1 and rep.profile["p"]["units"] == 2
    assert ledger.runs()[0]["report"]["fatal"] == "p: no connection"


def test_a_ledger_of_an_older_version_gets_the_new_columns(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "db")
    db.executescript("""CREATE TABLE ledger (step TEXT NOT NULL, unit TEXT NOT NULL, fingerprint TEXT,
        status TEXT, output_hash TEXT, attempts INTEGER DEFAULT 0, error TEXT, started TEXT, finished TEXT,
        PRIMARY KEY (step, unit));
        INSERT INTO ledger (step, unit, fingerprint, status) VALUES ('p', 'a', '1', 'ok');""")
    db.close()
    ledger = Ledger(tmp_path / "db")
    rep = Runner(ledger, [_step("p", ["a", "b"], {"a": "1", "b": "1"}, lambda ctx, u: None)]).run()
    assert rep.skipped == {"p": 1} and rep.processed == {"p": 1}
    assert ledger.get("p", "a")["seconds"] is None and ledger.get("p", "b")["seconds"] is not None


def test_step_workers_and_ctrl_c_with_units_in_other_threads(tmp_path):
    """Ctrl+C reaches only the main thread: the units that have not started are
    cancelled instead of waited for, and `stopping` tells the running ones to stop."""
    import time
    seen, crash = [], {"on": True}

    def run(ctx, u):
        if u == "u00" and crash["on"]:
            raise KeyboardInterrupt
        seen.append(u)
        time.sleep(0.05)

    units = [f"u{i:02d}" for i in range(20)]
    step = _step("p", units, {u: "1" for u in units}, run, workers=3)
    ledger = Ledger(tmp_path / "db")
    runner = Runner(ledger, [step], workers=1)
    with pytest.raises(KeyboardInterrupt):
        runner.run()
    assert runner.stopping.is_set() and len(seen) < 10 and ledger.get("p", "u00")["status"] == "running"
    crash["on"] = False
    done = len(seen)
    rep = Runner(ledger, [step], workers=1).run()
    assert rep.profile["p"]["workers"] == 3 and rep.processed == {"p": 20 - done} and rep.skipped == {"p": done}


def test_prepare_gets_the_pending_units_and_is_only_a_head_start(tmp_path):
    prepared, done = [], []
    fingerprints = {"a": "1", "b": "1", "c": "1"}
    behaviour = {"raise": None}

    def prepare(ctx, units):
        prepared.append(units)
        if behaviour["raise"]:
            raise behaviour["raise"]

    step = _step("p", ["a", "b", "c"], fingerprints, lambda ctx, u: done.append(u), prepare=prepare)
    ledger = Ledger(tmp_path / "db")
    rep = Runner(ledger, [step]).run()
    assert prepared == [["a", "b", "c"]] and rep.profile["p"]["prepare_seconds"] >= 0
    Runner(ledger, [step]).run()
    assert len(prepared) == 1                                 # nothing pending: not called
    fingerprints["b"] = "2"
    behaviour["raise"] = ValueError("the batch service is down")
    rep = Runner(ledger, [step]).run()                        # it fails: the unit does its own work
    assert prepared[-1] == ["b"] and rep.processed == {"p": 1} and done[-1] == "b"
    fingerprints["c"] = "2"
    behaviour["raise"] = FatalError("no connection")
    rep = Runner(ledger, [step]).run()
    assert rep.fatal == "p: no connection" and done[-1] == "b" and ledger.get("p", "c")["fingerprint"] == "1"


def test_dry_run_finds_the_pending_units_and_their_estimate_without_touching_anything(tmp_path):
    done, asked = [], []
    fingerprints = {"a": "1", "b": "1", "c": "1"}

    def estimate(ctx, units):
        asked.append(units)
        return {"requests": {"chat": 2 * len(units)}, "text": f"{2 * len(units)} chat requests"}

    first = _step("p", ["a", "b", "c"], fingerprints, lambda ctx, u: done.append(u), estimate=estimate)
    second = _step("q", ["x"], {"x": "1"}, lambda ctx, u: done.append(u), depends_on=["p"])
    ledger = Ledger(tmp_path / "db")
    Runner(ledger, [first]).run(only=["p"])
    fingerprints["b"] = "2"
    rows = {u: dict(r) for u, r in ledger.units("p").items()}
    with Runner(ledger, [first])._lock():              # another process runs: a dry run only looks
        rep = Runner(ledger, [first, second], dry_run=True).run()
    assert rep.pending == {"p": 1, "q": 1} and rep.skipped == {"p": 2} and rep.processed == {}
    assert asked[-1] == ["b"] and rep.estimate["p"]["requests"] == {"chat": 2}
    assert rep.estimate["q"] == {"after": ["p"]}         # q will have what p leaves to it
    assert done == ["a", "b", "c"] and {u: dict(r) for u, r in ledger.units("p").items()} == rows
    assert ledger.get("q", "x") is None and len(ledger.runs()) == 1   # no run saved

    rep = Runner(ledger, [first, second]).run()          # a real run asks too, and keeps it
    assert rep.pending == {"p": 1, "q": 1} and rep.estimate == {"p": {"requests": {"chat": 2},
                                                                     "text": "2 chat requests"}}
    assert rep.profile["p"]["estimate_seconds"] >= 0

    def broken(ctx, units):
        raise ValueError("cannot count")
    fingerprints["c"] = "2"
    rep = Runner(ledger, [_step("p", ["a", "b", "c"], fingerprints, lambda ctx, u: None, estimate=broken)]).run()
    assert rep.processed == {"p": 1} and rep.estimate == {}             # without an estimate, it goes on


def test_a_budget_of_requests_stops_cleanly_and_the_next_run_goes_on(tmp_path):
    meter, done = {"chat": 0}, []

    def run(ctx, u):
        meter["chat"] += 1                    # one request per unit
        done.append(u)

    units = [f"u{i}" for i in range(10)]
    first = _step("p", units, {u: "1" for u in units}, run, parallel=False)
    second = _step("q", ["x"], {"x": "1"}, run, depends_on=["p"])
    ledger = Ledger(tmp_path / "db")
    rep = Runner(ledger, [first, second], meters=lambda: dict(meter), max_requests=3).run()
    assert rep.stopped_by == "budget" and rep.processed == {"p": 3} and "q" not in rep.profile
    assert ledger.runs()[0]["report"]["stopped_by"] == "budget" and ledger.get("p", "u3") is None
    rep = Runner(ledger, [first, second], meters=lambda: dict(meter)).run()
    assert rep.stopped_by is None and rep.processed == {"p": 7, "q": 1} and rep.skipped == {"p": 3}
    assert done == units + ["x"]                                         # nothing repeated

    rep = Runner(ledger, [_step("r", ["a"], {"a": "1"}, run)], deadline=0).run()   # already past
    assert rep.stopped_by == "budget" and rep.processed == {} and ledger.get("r", "a") is None


def test_a_unit_stopped_halfway_by_the_budget_stays_running_and_is_done_again(tmp_path):
    """A long unit that watches `stopping` (a document between its sections) stops when the
    budget is spent while it runs, also with a single worker: Stopping looks at the budget
    whenever it is asked."""
    import time
    from atlas.pipeline import Interrupted, Stopping
    meter, parts = {"chat": 0}, []
    stopping = Stopping()

    def run(ctx, u):
        for k in range(5):
            if stopping.is_set():
                raise Interrupted(f"{u} interrupted")
            meter["chat"] += 1
            parts.append((u, k))

    step = _step("p", ["a", "b"], {"a": "1", "b": "1"}, run, parallel=False)
    ledger = Ledger(tmp_path / "db")
    rep = Runner(ledger, [step], meters=lambda: dict(meter), max_requests=7, stopping=stopping).run()
    assert rep.stopped_by == "budget" and rep.processed == {"p": 1} and rep.profile["p"]["interrupted"] == 1
    assert ledger.get("p", "b")["status"] == "running" and parts[-1] == ("b", 1)     # stopped at 7 requests
    assert stopping.check is None                     # the budget does not outlive the run
    rep = Runner(ledger, [step], meters=lambda: dict(meter), stopping=stopping).run()
    assert rep.processed == {"p": 1} and rep.skipped == {"p": 1} and ledger.get("p", "b")["status"] == "ok"

    # a deadline that passes while the unit runs
    def slow(ctx, u):
        for _ in range(100):
            if stopping.is_set():
                raise Interrupted(u)
            time.sleep(0.01)

    step = _step("s", ["a"], {"a": "1"}, slow)
    t0 = time.time()
    rep = Runner(ledger, [step], deadline=time.time() + 0.1, stopping=stopping).run()
    assert rep.stopped_by == "budget" and time.time() - t0 < 0.8 and ledger.get("s", "a")["status"] == "running"


def test_progress_lines_with_the_time_left(tmp_path):
    lines, meter = [], {"chat": 0}

    def run(ctx, u):
        meter["chat"] += 2

    units = [f"u{i:02d}" for i in range(12)]
    step = _step("p", units, {u: "1" for u in units}, run, parallel=False,
                 estimate=lambda ctx, pending: {"requests": {"chat": 2 * len(pending)}})
    runner = Runner(Ledger(tmp_path / "db"), [step], meters=lambda: dict(meter), progress=lines.append)
    runner.PROGRESS_UNITS = 5
    runner.run()
    assert lines == ["p 5/12 units, under 1 min, under 1 min left", "p 10/12 units, under 1 min, under 1 min left"]


def test_what_atlas_writes_has_no_carriage_returns(tmp_path, docs_samples):
    """On Windows, text written without newline="\\n" gets \\r\\n: the same workspace would
    have other bytes on each system."""
    atomic_write(tmp_path / "a.md", "line → one\nline two\n")
    assert (tmp_path / "a.md").read_bytes() == "line → one\nline two\n".encode("utf-8")
    from atlas.docs import DocsOptions, process_document
    process_document(docs_samples / "hmi_manual.md", tmp_path / "hmi", DocsOptions(analysis="none"))
    written = [p for p in (tmp_path / "hmi").rglob("*") if p.is_file()]
    assert len(written) > 6 and [p.name for p in written if b"\r" in p.read_bytes()] == []
