"""Minimal command line: the configuration lives in the TOML file.

    atlas run      [atlas.toml]     incremental, resumable processing
    atlas status   [atlas.toml]     state of the ledger and of the data
    atlas sections [atlas.toml]     sections each document would have, without the model
    atlas graph    [atlas.toml]     report of the graph (code, DOORS, documents) and of its gaps
    atlas trace    ID [atlas.toml]     everything about a requirement: DOORS, documents, code, tests
    atlas matrix   MODULE [atlas.toml] traceability matrix of a DOORS module (CSV and Markdown); * = all
    atlas search   TEXT [atlas.toml]   hybrid search in the indexed documents
    atlas ask      TEXT [atlas.toml]   answer from the indexed documents, with citations
    atlas plugins  [atlas.toml]     available plugins
    atlas agents   [atlas.toml]     agents (Markdown files): built-in and of the project
    atlas check    [atlas.toml]     connection test with the LLM and the embeddings
    atlas serve    [atlas.toml]     MCP server over stdio, for OpenCode, VS Code...
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


def index_requirements_step(ctx) -> Step:
    """One unit per DOORS module read by doors.requirements: its requirements are indexed
    as one more document ("doors:<module>"), so that search finds them too."""
    from atlas.index import DOORS_PREFIX, Index
    from atlas.llm import from_config
    kinds = index_options(ctx)["kinds"]
    embed_model = from_config(ctx.cfg).embeddings.model
    shared: dict = {}
    lock = threading.Lock()

    def index() -> Index:
        with lock:
            if "index" not in shared:
                shared["index"] = Index.from_config(ctx.cfg)
        return shared["index"]

    def units(ctx) -> list[str]:          # nothing to index if neither kind is wanted
        return ctx.store.modules() if {"requirement", "chunk"} & set(kinds) else []

    def fingerprint(ctx, module: str) -> str:
        rows = json.dumps(ctx.store.requirements(module), sort_keys=True, default=str)
        return f"{sha256_text(rows)}|{ctx.cfg.fingerprint('index')}|{embed_model}|{INDEX_VERSION}"

    def run(ctx, module: str):
        res = index().add_module(module, ctx.store.requirements(module), kinds)
        if res["missing"]:
            raise IndexIncomplete(f"{res['missing']} records got no vector ({len(res['failed_batches'])} "
                                  f"failed batches); they are embedded on the next run")
        return {"records": res["records"], "kinds": res["kinds"]}

    return Step(name="index.requirements", units=units, fingerprint=fingerprint, run=run,
                prune=lambda ctx, module: index().remove_document(DOORS_PREFIX + module),
                depends_on=["doors.requirements"])


GRAPH_VERSION = "graph-2"   # change it when the building of the graph changes
GRAPH_EXPORTS = ("graphml", "json")


def graph_step(ctx) -> Step:
    """One unit per part of the graph (its origin): "code" (code.parse), "doors"
    (doors.requirements) and "docs" (docs.process). Each one is rebuilt whole (seconds)
    when what it is built from changes; then the whole graph is exported."""
    from atlas.doors import normalize_id
    from atlas.graph import (GraphStore, build_code_graph, build_docs_graph, build_doors_graph, code_names,
                             document_names, export_graph, find_communities)
    exports = ctx.cfg["graph"]["exports"]
    unknown = sorted(set(exports) - set(GRAPH_EXPORTS))
    if unknown:
        raise ConfigError(f"[graph] unknown exports {unknown} (valid: {', '.join(GRAPH_EXPORTS)})")
    folder = ctx.cfg.workspace / "graph"
    test_patterns = (ctx.cfg["sources"].get("code") or {}).get("test_patterns", [])
    id_normalize = ((ctx.cfg["sources"].get("doors") or {}).get("convention") or {}).get("id_normalize", "upper")
    normalize = lambda rid: normalize_id(rid, id_normalize)
    settings = f"{ctx.cfg.fingerprint('graph')}|{test_patterns}|{id_normalize}|{GRAPH_VERSION}"

    def documents(ctx) -> list[dict]:
        docs = [d for d in ctx.store.documents() if (Path(d["out_dir"]) / "structure.json").exists()]
        names = document_names(docs)
        return [{**d, "name": names[d["unit"]],
                 "structure": json.loads((Path(d["out_dir"]) / "structure.json").read_text(encoding="utf-8"))}
                for d in docs]

    def units(ctx) -> list[str]:
        present = {"code": bool(ctx.store.code_units()), "doors": ctx.store.counts()["requirements"] > 0,
                   "docs": any((Path(d["out_dir"]) / "structure.json").exists() for d in ctx.store.documents())}
        return [origin for origin, there in present.items() if there]

    def fingerprint(ctx, unit: str) -> str:
        if unit == "code":
            data = ctx.store.code_units()
        elif unit == "doors":
            data = [ctx.store.requirements(), ctx.store.links()]
        else:
            docs = [d for d in ctx.store.documents() if (Path(d["out_dir"]) / "structure.json").exists()]
            data = [[d["unit"], d["path"], sha256_file(Path(d["out_dir"]) / "structure.json")] for d in docs]
            data.append(sorted(code_names(ctx.store.code_units()).items()))    # what the sections can mention
        return f"{sha256_text(json.dumps(data, sort_keys=True, default=str))}|{settings}"

    def build(ctx, unit: str):
        if unit == "code":
            G, stats = build_code_graph(ctx.store.code_units(), test_patterns, normalize)
            if ctx.cfg["graph"]["communities"]:          # in the node, for the exports (Gephi colours)
                groups = find_communities(G)
                for n, group in groups.items():
                    G.nodes[n]["community"] = group
                stats["communities"] = len(set(groups.values()))
            return G, stats
        if unit == "doors":
            return build_doors_graph(ctx.store.requirements(), ctx.store.links())
        return build_docs_graph(documents(ctx), code_names(ctx.store.code_units()), normalize)

    def export(store: GraphStore) -> None:
        for old in [*folder.glob("atlas.*"), *folder.glob("code.*")]:        # code.*: before graph-2
            old.unlink()
        if store.builds():
            stats = {o: {k: v for k, v in b.items() if not k.startswith("top_")} for o, b in store.builds().items()}
            export_graph(store.load(), folder / "atlas", exports, stats)

    def run(ctx, unit: str):
        G, stats = build(ctx, unit)
        store = GraphStore(ctx.store.path)
        store.replace(unit, G, stats)
        export(store)
        log.info("graph %s: %s", unit, ", ".join(f"{v} {k}" for k, v in stats.items() if isinstance(v, int)))
        return {k: v for k, v in stats.items() if not k.startswith("top_")}

    def prune(ctx, unit: str) -> None:
        store = GraphStore(ctx.store.path)
        store.delete(unit)
        export(store)

    return Step(name="graph.build", units=units, fingerprint=fingerprint, run=run, prune=prune,
                depends_on=["code.parse", "doors.requirements", "docs.process"], parallel=False)


def steps(ctx) -> list[Step]:
    wanted = ctx.cfg["pipeline"]["steps"]
    builders = {"doors": [doors_step], "code": [code_step], "docs": [docs_step], "graph": [graph_step],
                "index": [index_step, index_requirements_step]}
    out = []
    for name in wanted:
        if name not in builders:
            raise ConfigError(f"[pipeline] unknown step '{name}'")
        out += [s for s in (build(ctx) for build in builders[name]) if s]
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
    """The report of the last build of every part of the graph, and of its gaps."""
    from atlas.graph import GraphStore, KnowledgeGraph
    ctx = _context(cfg)
    builds = GraphStore(ctx.store.path).builds()
    if not builds:
        print("no graph yet: run `atlas run` with \"graph\" in [pipeline] steps")
        return 1
    s = builds.get("code")
    if s:
        print(f"code graph of {s['built_at'][:19]}: {s['nodes']} nodes, {s['edges']} edges ({s['subprograms']} "
              f"subprograms, {s['tests']} tests, {s['types']} types, {s['variables']} variables, {s['externals']} "
              f"externals, {s['requirements']} requirements) from {s['files']} files ({s['test_files']} of tests)")
        c = s["edges_by_kind"]
        print(f"calls: {c['calls']} | type conversions: {c['converts']} | global variables used: {c['uses']} | "
              f"predefined types ignored: {c['predefined']} | ambiguous calls: {s['ambiguous_calls']}")
        print(f"subprograms without a comment: {s['subprograms_without_doc']} | "
              f"without a requirement: {s['subprograms_without_requirement']} | "
              f"unresolved calls: {s['unresolved_calls']}"
              + (f" | groups of code (communities): {s['communities']}" if "communities" in s else ""))
        for title, key in (("Most called", "top_called"), ("Requirements traced by most elements", "top_requirements"),
                           ("Most frequent unresolved calls (external libraries or names not captured)",
                            "top_unresolved")):
            if s[key]:
                print(f"\n{title}:")
                for name, n in s[key]:
                    print(f"  {n:5d}  {name}")
    s = builds.get("doors")
    if s:
        print(f"\nDOORS of {s['built_at'][:19]}: {s['modules']} modules, {s['requirements']} requirements, "
              f"{s['headings']} headings; links: "
              + ", ".join(f"{n} {k}" for k, n in s["edges_by_kind"].items() if k != "contains")
              + (f"; {s['linked_outside']} IDs linked from modules that were not read" if s["linked_outside"] else ""))
    s = builds.get("docs")
    if s:
        print(f"\ndocuments of {s['built_at'][:19]}: {s['documents']} documents, {s['sections']} sections, "
              f"{s['requirements']} requirements stated, {s['mentions']} mentions of code elements")
    gaps = KnowledgeGraph.open(ctx.store.path).gaps()
    print("\ntraceability gaps (atlas matrix MODULE for the detail):")
    for key, value in gaps.items():
        if isinstance(value, dict):
            print(f"  {value['count']:5d}  {key.replace('_', ' ')}")
    print(f"\nexported to {cfg.workspace / 'graph'}")
    return 0


def trace(cfg: Config, req_id: str) -> int:
    from atlas.graph import KnowledgeGraph
    ctx = _context(cfg)
    res = KnowledgeGraph.open(ctx.store.path).trace(req_id)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if "error" in res else 0


def matrix(cfg: Config, module: str) -> int:
    """The traceability matrix of a module ("*": every requirement) in <workspace>/graph,
    as CSV (for a spreadsheet) and Markdown, and its summary and gaps on the screen."""
    import csv
    from atlas.graph import KnowledgeGraph
    ctx = _context(cfg)
    module = "" if module == "*" else module
    res = KnowledgeGraph.open(ctx.store.path).matrix(module, limit=10**9)
    if "error" in res:
        print(res["error"] + (f"; modules: {', '.join(res['modules'])}" if res.get("modules") else ""))
        return 1
    columns = ["id", "text", "status", "verification", "derived", "code", "tests", "covering_tests", "documents"]
    cell = lambda v: "; ".join(v) if isinstance(v, list) else str(v)
    prefix = cfg.workspace / "graph" / f"matrix-{module or 'all'}"
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with open(prefix.with_suffix(".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows([cell(row.get(c, "")) for c in columns] for row in res["rows"])
    sm, gaps = res["summary"], res["gaps"]
    md = [f"# Traceability matrix: {res['module']}", "",
          f"{sm['requirements']} requirements: {sm['with_code']} with code, {sm['with_tests']} with tests, "
          f"{sm['with_documents']} in documents.", "",
          "| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    md += ["| " + " | ".join(cell(row.get(c, "")).replace("|", "\\|") for c in columns) + " |" for row in res["rows"]]
    for key, title in (("without_code", "Without code"), ("without_tests", "Without tests")):
        md += ["", f"## {title} ({gaps[key]['count']})", "", ", ".join(gaps[key]["items"]) or "-"]
    prefix.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(md[2])
    for key, title in (("without_code", "without code"), ("without_tests", "without tests")):
        items = gaps[key]["items"]
        print(f"{title} ({gaps[key]['count']}): {', '.join(items[:20])}" + (" …" if len(items) > 20 else ""))
    print(f"written {prefix.with_suffix('.csv')} and {prefix.with_suffix('.md')}")
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


def list_agents(cfg: Config) -> int:
    from atlas.agents.definitions import BUILTIN, load_definitions
    _context(cfg)
    for d in load_definitions(cfg).values():
        source = "built-in" if Path(d.source).parent == BUILTIN else d.source
        print(f"{d.name:22s} {source}\n    {d.description}\n    tools: {', '.join(d.tools) or '-'}"
              f" | model: {d.model or cfg['agents']['model'] or cfg.get('llm', {}).get('model', '') or '-'}"
              f" | max_steps: {d.max_steps}")
    return 0


def check(cfg: Config) -> int:
    from atlas.llm import check as check_connection, from_config
    return check_connection(from_config(cfg))


def serve(cfg: Config) -> int:
    """The MCP server over stdio, until the client closes it."""
    from atlas.mcp import serve as serve_mcp
    try:
        serve_mcp(cfg, _context(cfg))
    except ImportError as e:            # without the mcp package
        sys.exit(str(e))
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    action, rest = argv[0], argv[1:]
    actions = {"run": run, "status": status, "sections": sections, "graph": graph, "plugins": list_plugins,
               "agents": list_agents, "check": check, "serve": serve}
    with_text = {"search": search, "ask": ask, "trace": trace, "matrix": matrix}
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
