"""Minimal command line: the configuration lives in the TOML file.

    atlas run      [atlas.toml]     incremental, resumable processing
    atlas status   [atlas.toml]     state of the ledger and of the data
    atlas plugins  [atlas.toml]     available plugins
    atlas check    [atlas.toml]     connection test with the LLM and the embeddings
    atlas serve    [atlas.toml]     (phase 5) MCP server
"""
from __future__ import annotations

import fnmatch
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

from atlas import plugins
from atlas.config import Config, ConfigError, load_config
from atlas.pipeline import FatalError, Ledger, Runner, Step, sha256_file
from atlas.store import Store

log = logging.getLogger("atlas")


def _context(cfg: Config) -> SimpleNamespace:
    logging.basicConfig(level=cfg["project"]["log_level"], format="%(levelname)s %(name)s: %(message)s")
    loaded = plugins.load_plugins(cfg["plugins"]["modules"])
    log.debug("plugins loaded: %s", loaded)
    return SimpleNamespace(cfg=cfg, store=Store(cfg.workspace), disabled=cfg["plugins"]["disabled"])


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def doors_step(ctx) -> Step | None:
    sd = ctx.cfg["sources"].get("doors")
    if not sd or not sd.get("enabled", True):
        return None
    adapter = plugins.get("requirements", sd.get("adapter", "database"), ctx.disabled)(sd)
    from atlas.doors import hash_requirement

    def run(ctx, module: str):
        reqs = list(adapter.requirements(module))
        links = list(adapter.links(module))
        hashes = {r.id: hash_requirement(r) for r in reqs}
        previous = ctx.store.requirement_hashes(module)
        ctx.store.replace_module(module, reqs, links, hashes)
        changes = {"added": len(set(hashes) - set(previous)), "removed": len(set(previous) - set(hashes)),
                   "changed": sum(1 for i, h in hashes.items() if i in previous and previous[i] != h)}
        log.info("doors %s: %d requirements, %d links (%s)", module, len(reqs), len(links), changes)
        return {"requirements": len(reqs), "links": len(links), **changes}

    return Step(
        name="doors.requirements",
        units=lambda ctx: adapter.modules(),
        fingerprint=lambda ctx, m: f"{adapter.snapshot(m)}|{ctx.cfg.fingerprint('sources.doors')}",
        run=run,
        prune=lambda ctx, m: ctx.store.delete_module(m),
        parallel=False,
    )


def code_step(ctx) -> Step | None:
    sc = ctx.cfg["sources"].get("code")
    if not sc or not sc.get("enabled", True):
        return None
    analyzers = {}
    for lang in sc.get("languages", ["ada"]):
        try:
            analyzers[lang] = plugins.get("code_analyzer", lang, ctx.disabled)(sc)
        except LookupError as e:
            raise ConfigError(str(e)) from None
    by_ext = {ext: a for a in analyzers.values() for ext in a.extensions}
    exclude = sc.get("exclude", [])

    def files(ctx) -> list[str]:
        out = []
        for root in sc.get("roots", []):
            for p in sorted(Path(root).rglob("*")):
                if p.is_file() and p.suffix.lower() in by_ext and \
                        not any(fnmatch.fnmatch(str(p).replace("\\", "/"), pat) for pat in exclude):
                    out.append(str(p))
        return out

    def run(ctx, f: str):
        a = by_ext[Path(f).suffix.lower()]
        unit = a.analyze_file(Path(f), Path(f).read_bytes())
        ctx.store.put_code_unit(unit)
        return {"unit": unit.unit, "subprograms": len(unit.subprograms)}

    return Step(
        name="code.parse",
        units=files,
        fingerprint=lambda ctx, f: f"{sha256_file(f)}|{by_ext[Path(f).suffix.lower()].version()}|"
                                   f"{ctx.cfg.fingerprint('sources.code')}",
        run=run,
        prune=lambda ctx, f: ctx.store.delete_code_unit(f),
    )


def steps(ctx) -> list[Step]:
    wanted = ctx.cfg["pipeline"]["steps"]
    builders = {"doors": doors_step, "code": code_step}
    out = []
    for name in wanted:
        if name in builders:
            s = builders[name](ctx)
            if s:
                out.append(s)
        elif name in ("docs", "graph", "index"):
            log.warning("step '%s' not migrated yet (see PLAN.md); skipped", name)
        else:
            raise ConfigError(f"[pipeline] unknown step '{name}'")
    return out


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def run(cfg: Config) -> int:
    ctx = _context(cfg)
    pc = cfg["pipeline"]
    runner = Runner(Ledger(ctx.store.path), steps(ctx), context=ctx, workers=pc["workers"],
                    on_error=pc["on_error"], max_attempts=pc["max_attempts"],
                    retry_quarantined=pc["retry_quarantined"], prune_missing=pc["prune_missing"])
    rep = runner.run()
    print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=1))
    if rep.fatal:
        print("Stopped by a fatal error; the state is saved: run `atlas run` again.", file=sys.stderr)
        return 2
    return 1 if rep.errors or rep.quarantined else 0


def status(cfg: Config) -> int:
    ctx = _context(cfg)
    ledger = Ledger(ctx.store.path)
    print(f"workspace: {cfg.workspace}")
    print("data:", ctx.store.counts())
    print("ledger:", json.dumps(ledger.summary(), ensure_ascii=False))
    pending = ledger.pending()
    for p in pending[:30]:
        print(f"  {p['status']:11s} {p['step']:20s} {p['unit'][:60]}  {p['error'] or ''}")
    if len(pending) > 30:
        print(f"  … and {len(pending) - 30} more")
    return 0


def list_plugins(cfg: Config) -> int:
    _context(cfg)
    for kind, names in plugins.describe().items():
        print(f"{kind:16s} {', '.join(names) or '-'}")
    return 0


def check(cfg: Config) -> int:
    from atlas.llm import check as check_connection, from_config
    return check_connection(from_config(cfg))


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    action, path = argv[0], (argv[1] if len(argv) > 1 else "atlas.toml")
    actions = {"run": run, "status": status, "plugins": list_plugins, "check": check}
    if action not in actions:
        sys.exit(f"unknown action '{action}'. {__doc__}")
    try:
        sys.exit(actions[action](load_config(path)))
    except ConfigError as e:
        sys.exit(f"configuration: {e}")
    except FatalError as e:
        sys.exit(f"fatal error: {e}")


if __name__ == "__main__":
    main()
