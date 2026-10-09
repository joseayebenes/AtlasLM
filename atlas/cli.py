"""Línea de comandos mínima: la configuración vive en el TOML.

    atlas run      [atlas.toml]     procesamiento incremental y reanudable
    atlas status   [atlas.toml]     estado del libro de registro y de los datos
    atlas plugins  [atlas.toml]     plugins disponibles
    atlas check    [atlas.toml]     prueba de conexión con el LLM y los embeddings
    atlas serve    [atlas.toml]     (fase 5) servidor MCP
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


def _contexto(cfg: Config) -> SimpleNamespace:
    logging.basicConfig(level=cfg["project"]["log_level"], format="%(levelname)s %(name)s: %(message)s")
    cargados = plugins.load_plugins(cfg["plugins"]["modules"])
    log.debug("plugins cargados: %s", cargados)
    return SimpleNamespace(cfg=cfg, store=Store(cfg.workspace), disabled=cfg["plugins"]["disabled"])


# --------------------------------------------------------------------------
# Pasos
# --------------------------------------------------------------------------

def paso_doors(ctx) -> Step | None:
    sd = ctx.cfg["sources"].get("doors")
    if not sd or not sd.get("enabled", True):
        return None
    adapter = plugins.get("requirements", sd.get("adapter", "database"), ctx.disabled)(sd)
    from atlas.doors import hash_requirement

    def run(ctx, module: str):
        reqs = list(adapter.requirements(module))
        links = list(adapter.links(module))
        hashes = {r.id: hash_requirement(r) for r in reqs}
        previos = ctx.store.requirement_hashes(module)
        ctx.store.replace_module(module, reqs, links, hashes)
        cambios = {"added": len(set(hashes) - set(previos)), "removed": len(set(previos) - set(hashes)),
                   "changed": sum(1 for i, h in hashes.items() if i in previos and previos[i] != h)}
        log.info("doors %s: %d requisitos, %d enlaces (%s)", module, len(reqs), len(links), cambios)
        return {"requirements": len(reqs), "links": len(links), **cambios}

    return Step(
        name="doors.requirements",
        units=lambda ctx: adapter.modules(),
        fingerprint=lambda ctx, m: f"{adapter.snapshot(m)}|{ctx.cfg.fingerprint('sources.doors')}",
        run=run,
        prune=lambda ctx, m: ctx.store.delete_module(m),
        parallel=False,
    )


def paso_code(ctx) -> Step | None:
    sc = ctx.cfg["sources"].get("code")
    if not sc or not sc.get("enabled", True):
        return None
    analizadores = {}
    for lang in sc.get("languages", ["ada"]):
        try:
            analizadores[lang] = plugins.get("code_analyzer", lang, ctx.disabled)(sc)
        except LookupError as e:
            raise ConfigError(str(e)) from None
    por_ext = {ext: a for a in analizadores.values() for ext in a.extensions}
    excluir = sc.get("exclude", [])

    def ficheros(ctx) -> list[str]:
        out = []
        for root in sc.get("roots", []):
            for p in sorted(Path(root).rglob("*")):
                if p.is_file() and p.suffix.lower() in por_ext and \
                        not any(fnmatch.fnmatch(str(p).replace("\\", "/"), pat) for pat in excluir):
                    out.append(str(p))
        return out

    def run(ctx, f: str):
        a = por_ext[Path(f).suffix.lower()]
        unit = a.analyze_file(Path(f), Path(f).read_bytes())
        ctx.store.put_code_unit(unit)
        return {"unit": unit.unit, "subprograms": len(unit.subprograms)}

    return Step(
        name="code.parse",
        units=ficheros,
        fingerprint=lambda ctx, f: f"{sha256_file(f)}|{por_ext[Path(f).suffix.lower()].version()}|"
                                   f"{ctx.cfg.fingerprint('sources.code')}",
        run=run,
        prune=lambda ctx, f: ctx.store.delete_code_unit(f),
    )


def pasos(ctx) -> list[Step]:
    quiere = ctx.cfg["pipeline"]["steps"]
    todos = {"doors": paso_doors, "code": paso_code}
    out = []
    for nombre in quiere:
        if nombre in todos:
            s = todos[nombre](ctx)
            if s:
                out.append(s)
        elif nombre in ("docs", "graph", "index"):
            log.warning("paso '%s' pendiente de migración (ver PLAN.md); se omite", nombre)
        else:
            raise ConfigError(f"[pipeline] paso desconocido '{nombre}'")
    return out


# --------------------------------------------------------------------------
# Acciones
# --------------------------------------------------------------------------

def run(cfg: Config) -> int:
    ctx = _contexto(cfg)
    pc = cfg["pipeline"]
    runner = Runner(Ledger(ctx.store.path), pasos(ctx), context=ctx, workers=pc["workers"],
                    on_error=pc["on_error"], max_attempts=pc["max_attempts"],
                    retry_quarantined=pc["retry_quarantined"], prune_missing=pc["prune_missing"])
    rep = runner.run()
    print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=1))
    if rep.fatal:
        print("Detenido por un error fatal; el estado está guardado: vuelve a ejecutar `atlas run`.", file=sys.stderr)
        return 2
    return 1 if rep.errors or rep.quarantined else 0


def status(cfg: Config) -> int:
    ctx = _contexto(cfg)
    ledger = Ledger(ctx.store.path)
    print(f"workspace: {cfg.workspace}")
    print("datos:", ctx.store.counts())
    print("registro:", json.dumps(ledger.summary(), ensure_ascii=False))
    pend = ledger.pending()
    for p in pend[:30]:
        print(f"  {p['status']:11s} {p['step']:20s} {p['unit'][:60]}  {p['error'] or ''}")
    if len(pend) > 30:
        print(f"  … y {len(pend) - 30} más")
    return 0


def listar_plugins(cfg: Config) -> int:
    _contexto(cfg)
    for tipo, nombres in plugins.describe().items():
        print(f"{tipo:16s} {', '.join(nombres) or '-'}")
    return 0


def check(cfg: Config) -> int:
    from atlas.llm import comprobar, desde_config
    return comprobar(desde_config(cfg))


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    accion, ruta = argv[0], (argv[1] if len(argv) > 1 else "atlas.toml")
    acciones = {"run": run, "status": status, "plugins": listar_plugins, "check": check}
    if accion not in acciones:
        sys.exit(f"acción desconocida '{accion}'. {__doc__}")
    try:
        sys.exit(acciones[accion](load_config(ruta)))
    except ConfigError as e:
        sys.exit(f"configuración: {e}")
    except FatalError as e:
        sys.exit(f"error fatal: {e}")


if __name__ == "__main__":
    main()
