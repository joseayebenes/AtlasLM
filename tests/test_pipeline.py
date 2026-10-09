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
    assert not (tmp_path / "db").with_suffix(".lock").exists()   # the lock is released
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
