"""The command line of hephesto: what writes and runs the code. The configuration is the
atlas.toml of the project, the same one atlas reads.

    hephesto develop PLAN [atlas.toml]   an agent writes the code task by task, the tests judge it: what
                     [--check] [--resume]  breaks is undone, and what it cannot fix goes to a person (a
                                         report); --check: the plan, check_code and the tests at the
                                         start, no model; --resume: goes on from where a run stopped
    hephesto hints   PLAN [atlas.toml]   drafts of hints for the errors of the compiler that came without
                                         one in the last hephesto develop of the plan, with how the agent
                                         fixed each one
    hephesto tests   ID|MODULE [atlas.toml]  the test procedures as tests in each language, step by step,
                                         by the patterns of [tests], without the model; * = all
    hephesto eval    [atlas.toml]        the evaluation sets of [agents.eval], with their develop cases
                     [--compare] [--set FILE]   (atlas eval, which leaves those out)
    hephesto serve   [atlas.toml]        MCP server over stdio for an editor: check_code, run_test, run_tests
                     [--edit]            and judge_changes; --edit: the file tools too (atlas serve, beside
                                         it, gives what atlas knows)
    hephesto --version

What atlas does (atlas run, trace, search, serve...) stays in atlas: `atlas --help`.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from atlas.cli import _context, _logged, _options
from atlas.cli import evaluate as atlas_evaluate
from atlas.config import Config, ConfigError, load_config
from atlas.pipeline import FatalError

__all__ = ["main"]

OPTIONS = {"develop": {"--check": bool, "--resume": bool},
           "eval": {"--compare": bool, "--set": str},
           "serve": {"--edit": bool}}


def tests(cfg: Config, target: str) -> int:
    """The tests of the test procedures of a module (or of one), from their steps."""
    from hephesto.procedures import generate_tests
    from atlas.graph import KnowledgeGraph
    ctx = _context(cfg)
    res = generate_tests(KnowledgeGraph.open(ctx.store.path, cfg.home), "" if target == "*" else target, cfg["tests"],
                         cfg.home)
    if "error" in res:
        print(res["error"] + (f"; modules: {', '.join(res['modules'])}" if res.get("modules") else ""))
        return 1
    for p in res["procedures"]:
        files = ", ".join(f"{f['path']} ({f['status']})" for f in p["files"])
        print(f"{p['id']}: {files}" if files else f"{p['id']}: not written")
        for e in p["errors"]:
            print(f"    {e}")
    print(f"{res['written']} written, {res['unchanged']} unchanged"
          + (f"; {res['errors']} procedures with steps that no pattern of [tests] steps matches" if res["errors"] else ""))
    return 1 if res["errors"] else 0


def _develop_check(cfg: Config, plan: str) -> int:
    """What a plan starts from, without a model: its agents and tests checked, check_code on
    its files and its tests run once (Developer.check)."""
    from hephesto.develop import Developer, load_plan
    found = Developer(load_plan(plan), cfg).check()
    print(f"plan {found['plan']}: {found['tasks']} tasks, {len(found['tests'])} tests; "
          + ", ".join(f"{k} {v}" for k, v in found["agents"].items()))
    print(f"check_code: {len(found['check_errors'])} files with errors" if found["check_errors"] else
          "check_code: no errors")
    for f, errors in found["check_errors"].items():
        print(f"  {f}: {errors[0]}" + (f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""))
    print(f"tests: {found['tests_passed']} of {len(found['tests'])} pass at the start")
    for test, result in found["tests"].items():
        print(f"  {test}: {result}")
    for warning in found["warnings"]:
        print(f"warning: {warning}")
    print("nothing was changed and no model was asked")
    return 0


def develop(cfg: Config, plan: str, check: bool = False, resume: bool = False) -> int:
    """The tasks of a plan, then the rounds that fix the failing tests (hephesto/develop.py);
    with `check`, only what the plan starts from; with `resume`, from where it stopped."""
    from hephesto.develop import develop as run_plan

    def show(s: dict) -> None:
        cost = f" ${s['cost']:.4f}" if "cost" in s else ""
        stop = " (stalled)" if s.get("stalled") else " (max_steps)" if s.get("stopped") else ""
        attempt = f" #{s['attempt']}" if s.get("attempt") else ""
        print(f"{s['n']:3d} {s['kind'] + attempt:10s} {s['task']}: {s.get('outcome', '')}, {s['seconds']} s, "
              f"{s['calls']} calls{stop}{cost}; tests passing: {s.get('tests_passed', '?')}"
              + (f"\n      broke: {'; '.join(s['broke'])[:300]}" if s.get("broke") else "")
              + (f"\n      did not advance: {', '.join(s['not_advanced'])}" if s.get("not_advanced") else "")
              + (f"\n      untagged: {', '.join(s['untagged'])}" if s.get("untagged") else "")
              + (f"\n      new files: {', '.join(s['created'])}" if s.get("created") else "")
              + (f"\n      deleted (a test among the code): {', '.join(s['deleted'])}" if s.get("deleted") else "")
              + (f"\n      error: {s['error']}" if s.get("error") else "")
              + (f"\n      budget spent: {s['budget_exceeded']}" if s.get("budget_exceeded") else "")
              + (f"\n      conflict: {', '.join(s['conflict']['files'])}"
                 + (f" (with {', '.join(s['conflict']['with'])})" if s["conflict"]["with"] else "")
                 + "; undone, it goes again alone" if s.get("conflict") else ""), flush=True)

    _context(cfg)
    try:
        if check:
            return _develop_check(cfg, plan)
        report = run_plan(plan, cfg, on_step=show, resume=resume)
    except ImportError as e:            # without the [agents] extra
        sys.exit(str(e))
    except KeyboardInterrupt:
        print(f"\ninterrupted: the files of the step are as they were before it; `hephesto develop {plan} "
              f"{cfg.path.name} --resume` goes on from there")
        return 130
    final, totals = report["final"], report["totals"]
    print(f"\n{final['tests_passed']} of {final['tests']} tests pass; {len(report['steps'])} agent runs, "
          f"{totals['seconds'] // 60} min of runs, {totals.get('wall_seconds', 0) // 60} min in all"
          + (f", ${totals['cost']}" if "cost" in totals else "")
          + (f"; {final['errors']} runs failed with an error" if final.get("errors") else ""))
    schedule = report.get("schedule")
    if schedule:
        print(f"tasks: {schedule['tasks_seconds'] // 60} min one after another in "
              f"{schedule['wall_seconds'] // 60} min, {schedule['speedup']:g}x as fast with {schedule['parallel']} at once"
              + (f"; {schedule['conflicts']} done again alone after a conflict" if schedule["conflicts"] else ""))
    for warning in report.get("warnings", []):
        print(f"warning: {warning}")
    for test, why in (report.get("for_a_person") or {}).items():
        print(f"for a person: {test} ({why['reason']})")
    created = [f for s in report["steps"] if s.get("outcome") not in ("undone", "not chosen")
               for f in s.get("created", [])]
    if created:
        print(f"new files: {', '.join(created)}")
    if report.get("proposal"):
        print(f"a proposal: the working folder is unchanged; the patches in {report['proposal']['patches']}, the "
              f"code in {report['proposal']['copy']}")
    print(f"report: {cfg.workspace / 'reports' / ('develop-' + report['plan'] + '.md')}")
    return 0 if final["tests_passed"] == final["tests"] else 1


def hints(cfg: Config, plan: str) -> int:
    """Drafts of hints from the calls of the last hephesto develop of a plan (hephesto/hints.py)."""
    from hephesto.hints import write_hints_draft
    calls = cfg.workspace / "reports" / f"develop-{Path(plan).stem}.calls.jsonl"
    if not calls.is_file():
        sys.exit(f"no calls of hephesto develop for {Path(plan).stem}: {calls} does not exist")
    path, drafts = write_hints_draft(calls)
    for d in drafts:
        fixed = f", fixed in run {d['fix']['run']}" if d["fix"] else ""
        print(f"{d['count']:4d}  {d['message'][:100]}{fixed}")
    print(f"\n{len(drafts)} errors without a hint; drafts in {path}: write the hints there and copy them to a "
          "file of [sources.code] hints" if drafts else "every error of the compiler came with a hint")
    return 0


def evaluate(cfg: Config, compare: bool = False, set: str = "") -> int:
    """atlas eval with the develop cases run (hephesto/evaluate.py)."""
    from hephesto.evaluate import RUNNERS
    return atlas_evaluate(cfg, compare=compare, set=set, runners=RUNNERS)


def serve(cfg: Config, edit: bool = False) -> int:
    """The MCP server of hephesto over stdio (hephesto/mcp.py), until the client closes it."""
    from hephesto.mcp import serve as serve_mcp
    try:
        serve_mcp(cfg, _context(cfg), edit=edit)
    except ImportError as e:            # without the mcp package
        sys.exit(str(e))
    return 0


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):        # a Windows console is cp1252 (see atlas.cli.main)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if argv[0] in ("-V", "--version", "version"):
        from atlas import __version__ as atlas_version
        from hephesto import __version__
        print(f"hephesto {__version__} (atlas {atlas_version})")
        sys.exit(0)
    action, rest = argv[0], argv[1:]
    from atlas.llm import set_usage_label
    set_usage_label(action)
    t0 = time.monotonic()
    try:
        _dispatch(action, rest)
    finally:
        import logging
        error = sys.exc_info()[1]
        how = f"exit code {error.code}" if isinstance(error, SystemExit) else \
            f"{type(error).__name__}: {error}" if error else "exit code 0"
        logging.getLogger("hephesto").debug("end of %s: %s, %.1f s", action, how, time.monotonic() - t0)


def _dispatch(action: str, rest: list[str]) -> None:
    actions = {"eval": evaluate, "serve": serve}
    with_text = {"develop": develop, "hints": hints, "tests": tests}
    if action not in actions and action not in with_text:
        sys.exit(f"unknown action '{action}'. {__doc__}")
    rest, options = _options(action, rest, OPTIONS, "hephesto")
    if action in with_text and not rest:
        sys.exit(f"usage: hephesto {action} TEXT [atlas.toml]")
    try:
        if action in with_text:
            cfg = load_config(rest[1] if len(rest) > 1 else "atlas.toml")
            sys.exit(_logged(action, with_text[action], cfg)(cfg, rest[0], **options))
        cfg = load_config(rest[0] if rest else "atlas.toml")
        sys.exit(_logged(action, actions[action], cfg)(cfg, **options))
    except ConfigError as e:
        sys.exit(f"configuration: {e}")
    except FatalError as e:
        sys.exit(f"fatal error: {e}")


if __name__ == "__main__":
    main()
