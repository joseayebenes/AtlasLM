"""Minimal command line: the configuration lives in the TOML file.

    atlas run      [atlas.toml]     incremental, resumable processing
    atlas status   [atlas.toml]     state of the ledger and of the data
    atlas sections [atlas.toml]     sections each document would have, without the model
    atlas graph    [atlas.toml]     report of the code graph (calls, requirements, unresolved)
    atlas search   TEXT [atlas.toml]   hybrid search in the indexed documents
    atlas ask      TEXT [atlas.toml]   answer from the indexed documents, with citations
    atlas plugins  [atlas.toml]     available plugins
    atlas check    [atlas.toml]     connection test with the LLM and the embeddings
    atlas serve    [atlas.toml]     (phase 5) MCP server
"""
from __future__ import annotations

import fnmatch
import json
import logging
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from atlas import plugins
from atlas.config import Config, ConfigError, load_config
from atlas.pipeline import FatalError, Ledger, Runner, Step, sha256_file, sha256_text
from atlas.store import Store

log = logging.getLogger("atlas")


def _context(cfg: Config) -> SimpleNamespace:
    logging.basicConfig(level=cfg["project"]["log_level"], format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)     # one line per request otherwise
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


class AnalysisIncomplete(RuntimeError):
    """Some sections of a document (or its overview) could not be analysed. The output is
    written; the unit stays in error so that the next run retries them."""


DOCS_VERSION = "docs-1"     # change it when the processing of documents changes


def doc_sources(ctx) -> list[tuple[dict, "DocsOptions"]]:
    """The enabled [[sources.docs]] with their validated options."""
    from atlas.docs import DocsOptions
    out, names = [], set()
    for d in ctx.cfg["sources"].get("docs", []):
        if not d.get("enabled", True):
            continue
        if d["name"] in names:
            raise ConfigError(f"[sources.docs] the name '{d['name']}' is repeated")
        names.add(d["name"])
        try:
            out.append((d, DocsOptions.from_source(d)))
        except ValueError as e:
            raise ConfigError(f"[sources.docs] '{d['name']}': {e}") from None
    return out


def doc_files(ctx, source: dict) -> list[Path]:
    """Files of a docs source: its paths (files, or folders searched recursively with its
    patterns), without anything inside the workspace."""
    from atlas.docs import extractor_for
    patterns = source.get("patterns") or [
        f"*{ext}" for name in plugins.available("extractor") if f"extractor:{name}" not in ctx.disabled
        for ext in plugins.get("extractor", name).extensions]
    found: set[Path] = set()
    for root in source["paths"]:
        p = Path(root)
        if p.is_file():
            found.add(p)
        elif p.is_dir():
            found.update(f for pat in patterns for f in p.rglob(pat) if f.is_file())
        else:
            log.warning("[sources.docs] '%s': %s does not exist", source["name"], root)
    ws = ctx.cfg.workspace
    out = []
    for f in sorted(found):
        if f.resolve().is_relative_to(ws):
            continue
        try:
            extractor_for(f, ctx.disabled)
        except LookupError as e:
            log.warning("%s; skipped", e)
            continue
        out.append(f)
    return out


def docs_step(ctx) -> Step | None:
    sources = doc_sources(ctx)
    if not sources:
        return None
    from atlas.docs import process_document
    from atlas.docs.analysis import PROMPT_VERSION
    from atlas.llm import JsonLlm, from_config
    by_name = {d["name"]: (d, options) for d, options in sources}
    root = ctx.cfg.workspace / "docs"
    model = from_config(ctx.cfg).llm.model
    shared: dict = {}

    def llm() -> JsonLlm:
        if "llm" not in shared:        # only when some source uses it, and only once
            shared["llm"] = JsonLlm(from_config(ctx.cfg).llm)
        return shared["llm"]

    def units(ctx) -> list[str]:
        return [f"{d['name']}:{f}" for d, _ in sources for f in doc_files(ctx, d)]

    def fingerprint(ctx, unit: str) -> str:
        name, f = unit.split(":", 1)
        d, options = by_name[name]
        return (f"{sha256_file(f)}|{sha256_text(json.dumps(d, sort_keys=True))[:16]}|{DOCS_VERSION}|"
                + (f"{model}|{PROMPT_VERSION}" if options.analysis == "llm" else ""))

    def out_dir(ctx, unit: str) -> Path:
        """<workspace>/docs/<source>/<name>_<extension>, unless another file already has it."""
        name, f = unit.split(":", 1)
        known = ctx.store.document(unit)
        if known:
            return Path(known["out_dir"])
        out = root / name / f"{Path(f).stem}_{Path(f).suffix.lstrip('.').lower()}"
        if ctx.store.document_owning(str(out)) not in (None, unit):
            out = out.with_name(f"{out.name}-{sha256_text(unit)[:8]}")
        return out

    def run(ctx, unit: str):
        name, f = unit.split(":", 1)
        d, options = by_name[name]
        out = out_dir(ctx, unit)
        data = process_document(Path(f), out, options, llm=llm() if options.analysis == "llm" else None,
                                cache_dir=root / ".cache", workers=ctx.cfg["pipeline"]["workers"],
                                disabled=ctx.disabled)
        summary = {"sections": len(data["sections"]), "requirements": len(data["requirements"]),
                   "errors": data["errors"], "structure": data["structure_info"]["source"]}
        ctx.store.put_document(unit, name, f, str(out), data["title"], summary)
        if data["errors"]:
            raise AnalysisIncomplete(f"{data['errors']} sections (or the overview) could not be analysed; "
                                     f"they are retried on the next run (details in {out}/structure.json)")
        return summary

    def prune(ctx, unit: str) -> None:
        known = ctx.store.document(unit)
        if known:
            shutil.rmtree(known["out_dir"], ignore_errors=True)
            ctx.store.delete_document(unit)

    # sections are analysed in parallel inside each document (process_document's workers)
    return Step(name="docs.process", units=units, fingerprint=fingerprint, run=run, prune=prune,
                parallel=False)


class IndexIncomplete(RuntimeError):
    """Some records of a document got no vector. The rest is indexed; the unit stays in
    error so that the next run embeds what is missing."""


INDEX_VERSION = "index-1"   # change it when the records or the index tables change


def index_options(ctx) -> dict:
    """The validated [index] options for index_document."""
    from atlas.index import KINDS
    ic = ctx.cfg["index"]
    unknown = sorted(set(ic["kinds"]) - set(KINDS))
    if unknown:
        raise ConfigError(f"[index] unknown kinds {unknown} (valid: {', '.join(KINDS)})")
    if ic["chunk_chars"] < 200 or not 0 <= ic["chunk_overlap"] < ic["chunk_chars"] // 2:
        raise ConfigError("[index] chunk_chars must be at least 200 and chunk_overlap between 0 and "
                          "half of chunk_chars")
    return {"chunk_chars": ic["chunk_chars"], "chunk_overlap": ic["chunk_overlap"], "kinds": ic["kinds"]}


def index_step(ctx) -> Step:
    """One unit per processed document (docs.process): its structure.json is indexed."""
    from atlas.index import Index
    from atlas.llm import from_config
    options = index_options(ctx)
    embed_model = from_config(ctx.cfg).embeddings.model
    shared: dict = {}
    lock = threading.Lock()            # documents are indexed in parallel

    def index() -> Index:              # created on first use: no embeddings client otherwise
        with lock:
            if "index" not in shared:
                shared["index"] = Index.from_config(ctx.cfg)
        return shared["index"]

    def structure(unit: str) -> Path:
        return Path(ctx.store.document(unit)["out_dir"]) / "structure.json"

    def units(ctx) -> list[str]:
        return [d["unit"] for d in ctx.store.documents() if (Path(d["out_dir"]) / "structure.json").exists()]

    def fingerprint(ctx, unit: str) -> str:
        return f"{sha256_file(structure(unit))}|{ctx.cfg.fingerprint('index')}|{embed_model}|{INDEX_VERSION}"

    def run(ctx, unit: str):
        d = ctx.store.document(unit)
        name = Path(d["path"]).name             # what citations show, unless another document has it
        if any(x["name"] == name and x["doc"] != unit for x in index().documents()):
            name = d["path"]
        res = index().add_document(json.loads(structure(unit).read_text(encoding="utf-8")), doc=unit,
                                   name=name, out_dir=d["out_dir"], **options)
        if res["missing"]:
            raise IndexIncomplete(f"{res['missing']} records got no vector ({len(res['failed_batches'])} "
                                  f"failed batches); they are embedded on the next run")
        return {"records": res["records"], "kinds": res["kinds"]}

    return Step(name="index.documents", units=units, fingerprint=fingerprint, run=run,
                prune=lambda ctx, unit: index().remove_document(unit), depends_on=["docs.process"])


GRAPH_VERSION = "graph-1"   # change it when the building of the graph changes
GRAPH_EXPORTS = ("graphml", "json")


def graph_step(ctx) -> Step:
    """One unit, "code": the code graph is rebuilt whole (seconds) when any file changes."""
    from atlas.graph import GraphStore, build_code_graph, export_graph
    exports = ctx.cfg["graph"]["exports"]
    unknown = sorted(set(exports) - set(GRAPH_EXPORTS))
    if unknown:
        raise ConfigError(f"[graph] unknown exports {unknown} (valid: {', '.join(GRAPH_EXPORTS)})")
    prefix = ctx.cfg.workspace / "graph" / "code"

    def fingerprint(ctx, unit: str) -> str:
        units = ctx.store.code_units()
        return f"{sha256_text(json.dumps(units, sort_keys=True))}|{ctx.cfg.fingerprint('graph')}|{GRAPH_VERSION}"

    def run(ctx, unit: str):
        G, stats = build_code_graph(ctx.store.code_units())
        GraphStore(ctx.store.path).replace("code", G, stats)
        for old in prefix.parent.glob(f"{prefix.name}.*"):
            old.unlink()
        export_graph(G, prefix, exports, stats)
        log.info("code graph: %d nodes, %d edges (%d subprograms, %d requirements, %d unresolved calls)",
                 stats["nodes"], stats["edges"], stats["subprograms"], stats["requirements"],
                 stats["unresolved_calls"])
        return {k: v for k, v in stats.items() if not k.startswith("top_")}

    def prune(ctx, unit: str) -> None:
        GraphStore(ctx.store.path).delete("code")
        for old in prefix.parent.glob(f"{prefix.name}.*"):
            old.unlink()

    return Step(name="graph.build", units=lambda ctx: ["code"] if ctx.store.code_units() else [],
                fingerprint=fingerprint, run=run, prune=prune, depends_on=["code.parse"], parallel=False)


def steps(ctx) -> list[Step]:
    wanted = ctx.cfg["pipeline"]["steps"]
    builders = {"doors": doors_step, "code": code_step, "docs": docs_step, "graph": graph_step,
                "index": index_step}
    out = []
    for name in wanted:
        if name in builders:
            s = builders[name](ctx)
            if s:
                out.append(s)
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
    from atlas.graph import GraphStore
    from atlas.index import IndexStore
    print("graph:", GraphStore(ctx.store.path).counts())
    print("index:", IndexStore(ctx.store.path).counts())
    print("ledger:", json.dumps(ledger.summary(), ensure_ascii=False))
    pending = ledger.pending()
    for p in pending[:30]:
        print(f"  {p['status']:11s} {p['step']:20s} {p['unit'][:60]}  {p['error'] or ''}")
    if len(pending) > 30:
        print(f"  … and {len(pending) - 30} more")
    return 0


def sections(cfg: Config) -> int:
    """The sections of every document of [[sources.docs]], without analysing them: to
    tune structure, heading_regex, max_chars and min_chars before a run."""
    from atlas.docs import preview
    ctx = _context(cfg)
    for d, options in doc_sources(ctx):
        for f in doc_files(ctx, d):
            p = preview(f, options, ctx.disabled)
            print(f"\n[{d['name']}] {f} — '{p['title']}': {p['chars']} chars, {len(p['sections'])} sections")
            print(f"  structure: {p['structure']}"
                  + (f"; {p['subdivided']} large sections subdivided by their headings" if p["subdivided"] else "")
                  + (f"; {p['removed_lines']} header/footer lines removed" if p["removed_lines"] else ""))
            for s in p["sections"]:
                print(f"  {s['id']}  p.{s['pages'] or '?':>7}  {s['chars']:6d} chars  {' > '.join(s['path'])}"
                      + (f"  + {s['includes']}" if s["includes"] else ""))
            for title, n in p["oversized"][:10]:
                print(f"  ! '{title[:60]}' is split in {n} parts by size")
            if p.get("headings"):
                print("  lines that start like a numbered heading:")
                for reason, lines in sorted(p["headings"].items(), key=lambda kv: -len(kv[1])):
                    print(f"    {len(lines):5d}  {reason}" + "".join(f"\n           {x}" for x in lines[:3]))
                print("  If your headings have another format, set heading_regex (group 1 = number, "
                      "group 2 = title).")
    return 0


def graph(cfg: Config) -> int:
    """The report of the last build of the code graph."""
    from atlas.graph import GraphStore
    ctx = _context(cfg)
    s = GraphStore(ctx.store.path).builds().get("code")
    if not s:
        print("no code graph yet: run `atlas run` with \"code\" and \"graph\" in [pipeline] steps")
        return 1
    print(f"code graph of {s['built_at']}: {s['nodes']} nodes, {s['edges']} edges ({s['subprograms']} subprograms, "
          f"{s['types']} types, {s['variables']} variables, {s['externals']} externals, "
          f"{s['requirements']} requirements) from {s['files']} files")
    c = s["edges_by_kind"]
    print(f"calls: {c['calls']} | type conversions: {c['converts']} | global variables used: {c['uses']} | "
          f"predefined types ignored: {c['predefined']} | ambiguous calls: {s['ambiguous_calls']}")
    print(f"subprograms without a comment: {s['subprograms_without_doc']} | "
          f"without a requirement: {s['subprograms_without_requirement']} | unresolved calls: {s['unresolved_calls']}")
    for title, key in (("Most called", "top_called"), ("Requirements traced by most elements", "top_requirements"),
                       ("Most frequent unresolved calls (external libraries or names not captured)",
                        "top_unresolved")):
        if s[key]:
            print(f"\n{title}:")
            for name, n in s[key]:
                print(f"  {n:5d}  {name}")
    print(f"\nexported to {cfg.workspace / 'graph'}")
    return 0


def search(cfg: Config, query: str) -> int:
    from atlas.index import Index, cite
    _context(cfg)
    hits = Index.from_config(cfg).search(query, k=8)
    if not hits:
        print("nothing found (has `atlas run` indexed any document?)")
    for i, h in enumerate(hits, 1):
        print(f"\n[{i}] {cite(h)}\n    {h['text'][:400]}")
    return 0


def ask(cfg: Config, question: str) -> int:
    from atlas.index import Index
    _context(cfg)
    res = Index.from_config(cfg).answer(question)
    print(res["answer"])
    if res["sources"]:
        print("\nSources:")
    for s in res["sources"]:
        print(f"  [{s['n']}] {s['cite']}" + (f"\n       {s['file']}" if s["file"] else ""))
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
    action, rest = argv[0], argv[1:]
    actions = {"run": run, "status": status, "sections": sections, "graph": graph, "plugins": list_plugins,
               "check": check}
    with_text = {"search": search, "ask": ask}
    if action not in actions and action not in with_text:
        sys.exit(f"unknown action '{action}'. {__doc__}")
    if action in with_text and not rest:
        sys.exit(f"usage: atlas {action} TEXT [atlas.toml]")
    try:
        if action in with_text:
            sys.exit(with_text[action](load_config(rest[1] if len(rest) > 1 else "atlas.toml"), rest[0]))
        sys.exit(actions[action](load_config(rest[0] if rest else "atlas.toml")))
    except ConfigError as e:
        sys.exit(f"configuration: {e}")
    except FatalError as e:
        sys.exit(f"fatal error: {e}")


if __name__ == "__main__":
    main()
