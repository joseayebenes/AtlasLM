"""The develop cases of an evaluation set: a plan of hephesto develop on a copy of its
starting folder, scored by the tests that pass at the end (atlas.agents.evaluate has the other
kinds, and the format of a set).

    - id: calc-from-start                    # a plan of hephesto develop (needs the model and [agents])
      develop: hephestolm/examples/develop/calc/plan.toml
      from: hephestolm/examples/develop/calc/start   # copied to <workspace>/eval before each run
      expect_tests_passed: 2                 # it passes with at least these tests passing at the end
      runs: 1                                # optional: [agents.eval] runs otherwise
      config: hephestolm/examples/develop/calc/atlas.toml   # optional: the atlas.toml next to the plan

The folder of `from` goes to a temporary one in <workspace>/eval, hephesto develop runs there (its
paths are relative to it) with [sources.code] and the [agents] dirs of `config` over those of the
configuration evaluated, which gives the model; ATLAS_PYTHON is the Python of atlas, for the
commands of a Python project. Of each run it keeps the tests that pass, the edits made and
failed, the compilations with errors, the calls, the tokens, the time and the cost, and the
report of hephesto develop goes to <workspace>/reports/eval-<set>/<case>-<run>.md. The plans take
minutes or hours with a real model, so they go in a set of their own, not in the default ones:
hephesto eval --set FILE.

    from atlas.agents.evaluate import evaluate_sets
    evaluate_sets(cfg, runners={"develop": evaluate_develop})
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from atlas.config import Config, ConfigError

__all__ = ["RUNNERS", "evaluate_develop"]


@contextmanager
def _inside(folder: Path):
    """The working directory in `folder` while the block runs."""
    before = Path.cwd()
    os.chdir(folder)
    try:
        yield
    finally:
        os.chdir(before)


def _develop_config(cfg: Config, case: dict, plan: Path, workspace: Path, home: Path) -> Config:
    """The configuration of a run of a develop case: that evaluated (the model, the network),
    with [sources.code] and the [agents] dirs of the case (its `config`, or the atlas.toml next
    to its plan), and its own workspace. It is that of `home`, the copy of the starting
    folder: the relative paths of the code and of the plan are relative to it."""
    import copy
    from atlas.config import load_config
    data = copy.deepcopy(cfg.data)
    data["project"]["workspace"] = str(workspace)
    data["agents"]["dirs"] = [str(Path(d).expanduser() if Path(d).expanduser().is_absolute() else cfg.path.parent / d)
                              for d in data["agents"].get("dirs", [])]
    own = cfg.path.parent / case["config"] if case.get("config") else plan.parent / "atlas.toml"
    if own.exists():
        os.environ.setdefault("ATLAS_PYTHON", sys.executable)
        project = load_config(own)
        if project["sources"].get("code"):
            data["sources"]["code"] = project["sources"]["code"]
        dirs = [Path(d) if Path(d).is_absolute() else own.parent / d for d in project["agents"].get("dirs", [])]
        data["agents"]["dirs"] += [str(d) for d in dirs if d.is_dir()]       # over those evaluated
    elif case.get("config"):
        raise ConfigError(f"develop case {case['id']}: its config {own} does not exist")
    return Config(path=home / cfg.path.name, data=data, unset=cfg.unset)


def evaluate_develop(cases: list[dict], cfg: Config, runs: int = 1, client: Any = None,
                     on_result: Callable[[dict], None] | None = None, reports: Path | None = None) -> dict:
    """Runs every develop case (the others are left out) `runs` times, or those of the case, on
    a copy of its starting folder (see the module). {"results": [...], "passed", "total",
    "rate", "by_case", "totals"}; each result has the tests passing at the end, the edits,
    compilations, calls, tokens, seconds and cost of the run. `reports`, a folder, gets the
    report of each run (<case>-<run>.md and .json)."""
    from hephesto.develop import develop, load_plan
    from atlas.agents.runtime import agent_service, definition
    cases = [c for c in cases if c.get("kind") == "develop"]
    results = []
    for case in cases:
        plan = (cfg.path.parent / case["develop"]).resolve()
        start = (cfg.path.parent / case["from"]).resolve()
        if not plan.is_file() or not start.is_dir():
            raise ConfigError(f"develop case {case['id']}: " + (f"no plan {plan}" if not plan.is_file()
                                                                else f"no folder {start} to start from"))
        for n in range(1, int(case.get("runs") or runs) + 1):
            (cfg.workspace / "eval").mkdir(parents=True, exist_ok=True)
            folder = Path(tempfile.mkdtemp(prefix=f"{case['id']}-", dir=cfg.workspace / "eval"))
            project = folder / start.name
            shutil.copytree(start, project, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            run_cfg = _develop_config(cfg, case, plan, folder / "workspace", project)
            t0 = time.time()
            result = {"id": case["id"], "kind": "develop", "run": n, "expect_tests_passed": case["expect_tests_passed"]}
            try:
                agent = load_plan(plan).agent
                result["model"] = agent_service(definition(agent, run_cfg), run_cfg).model
                with _inside(project):
                    report = develop(plan, run_cfg, client=client)
                final, totals = report["final"], report["totals"]
                result.update({
                    "tests_passed": final["tests_passed"], "tests": final["tests"],
                    "edits": totals["edits"], "compiles": totals["compiles"], "calls": totals["calls"],
                    "agent_runs": len(report["steps"]), "agent_seconds": totals["seconds"],
                    "tokens": {"prompt": totals["prompt_tokens"], "completion": totals["completion_tokens"]},
                    **({"cost": totals["cost"]} if "cost" in totals else {}),
                    "undone": sum(1 for s in report["steps"] if s.get("broke")),
                    "for_a_person": sorted(report.get("for_a_person") or {})})
                if reports is not None:
                    reports.mkdir(parents=True, exist_ok=True)
                    source = run_cfg.workspace / "reports" / f"develop-{report['plan']}"
                    for suffix in (".md", ".json"):
                        shutil.copy(source.with_suffix(suffix), reports / f"{case['id']}-{n}{suffix}")
            except Exception as e:                       # a run that fails is a failed case, not the end
                result["error"] = f"{type(e).__name__}: {e}"[:300]
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            result["seconds"] = round(time.time() - t0, 1)
            result["passed"] = "error" not in result and result["tests_passed"] >= case["expect_tests_passed"]
            results.append(result)
            if on_result:
                on_result(result)
    passed = sum(r["passed"] for r in results)
    total = lambda key, field="": sum((r.get(key) or {}).get(field, 0) if field else r.get(key, 0) for r in results)
    return {"results": results, "passed": passed, "total": len(results),
            "rate": round(passed / len(results), 3) if results else 0.0,
            "by_case": {c["id"]: f"{sum(r['passed'] for r in results if r['id'] == c['id'])}/"
                                 f"{sum(1 for r in results if r['id'] == c['id'])}" for c in cases},
            "totals": {"tests_passed": total("tests_passed"), "tests": total("tests"),
                       "edits": {"made": total("edits", "made"), "failed": total("edits", "failed")},
                       "compiles": {"ok": total("compiles", "ok"), "failed": total("compiles", "failed")},
                       "calls": total("calls"), "seconds": round(total("seconds"), 1),
                       **({"cost": round(total("cost"), 4)} if any("cost" in r for r in results) else {})}}


RUNNERS = {"develop": evaluate_develop}      # the runners of atlas.agents.evaluate.evaluate_sets
