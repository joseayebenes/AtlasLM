"""Minimal command line: the configuration lives in the TOML file.

    atlas run      [atlas.toml]     incremental, resumable processing; with a budget it stops cleanly
                   [--until HH:MM] [--max-minutes N] [--max-requests N]   (exit 3) and goes on next time
    atlas estimate [atlas.toml]     what `atlas run` would do now: units, requests to the APIs and time,
                   [--latency S]    with the seconds per request of the last run (or S)
    atlas status   [atlas.toml]     state of the ledger and of the data, what is left to do and what went
                                    to each model
    atlas sections [atlas.toml]     sections each document would have, without the model
    atlas graph    [atlas.toml]     report of the graph (code, DOORS, documents) and of its gaps
    atlas trace    ID [atlas.toml]     everything about a requirement in Markdown: DOORS, documents (with the
                                       values that differ), code, tests, callers and source
    atlas matrix   MODULE [atlas.toml] traceability matrix of a DOORS module ([graph] matrix_formats: CSV
                                       for Excel, Markdown, JSON, XLSX); * = all
    atlas verify   ID|MODULE [atlas.toml]  the requirements against the documents, with the model; * = all
    atlas comply   ID|MODULE [atlas.toml]  whether the code of each requirement does all it asks, obligation
                                           by obligation, with the model; * = all
    atlas link     ID|MODULE [atlas.toml]  code that may implement the requirements without code, with the
                                           model; a report of proposals, the code is not changed; * = all
    atlas tests    ID|MODULE [atlas.toml]  the test procedures as tests in each language, step by step, by
                                           the patterns of [tests], without the model; * = all
    atlas impact   NAME [atlas.toml]   report of what changing a code element or a requirement affects
    atlas coverage MODULE [atlas.toml] report of the traceability gaps of a DOORS module; * = all
    atlas search   TEXT [atlas.toml]   hybrid search in the indexed documents, requirements and code
                   [--k N] [--kind KIND] [--document NAME] [--module MODULE] [--chapter CHAPTER]
                   [--attribute NAME=VALUE]   e.g. --attribute status=Approved (repeatable, or ";")
    atlas ask      TEXT [atlas.toml]   answer from the indexed documents, with citations; a broad question
                   [--k N] [--kind KIND] [--document NAME] [--whole]   reads its documents whole
                   [--module MODULE] [--chapter CHAPTER] [--attribute NAME=VALUE]
                   [--section S]   reads whole only a section (its number, title or ID)
                   [--dry-run]   only the plan of reading them whole, without asking the model
    atlas plugins  [atlas.toml]     available plugins, and the code roots and how many Ada files they have
                                    (those of the GPR of [sources.code] project, if any)
    atlas agents   [atlas.toml]     agents (Markdown files): built-in and of the project
    atlas agent    NAME TEXT [atlas.toml]   asks an agent (with its tools); prints the tools it called
    atlas develop  PLAN [atlas.toml]   an agent writes the code task by task, the tests judge it: what breaks
                   [--check] [--resume]  is undone, and what it cannot fix goes to a person (a report);
                                       --check: the plan, check_code and the tests at the start, no model;
                                       --resume: goes on from where an earlier run stopped (Ctrl+C)
    atlas hints    PLAN [atlas.toml]   drafts of hints for the errors of the compiler that came without one in
                                       the last atlas develop of the plan, with how the agent fixed each one
    atlas eval     [atlas.toml]     the evaluation sets of [agents.eval]: answers scored without a model
                   [--compare]      and each case against the last report of another version of atlas
                   [--generate FILE [--n 50]]   writes a set of search and graph cases from the index and the
                                    graph of the workspace, the same each time (instead of evaluating)
                   [--set FILE]     only that set (examples/eval/develop.yaml: plans of atlas develop)
    atlas usage    [atlas.toml] [--since 30d]   what the usage log of the workspace says: calls by tool
                                    and week, milliseconds, errors, tokens and cost, answers with
                                    and without sources
    atlas check    [atlas.toml]     connection test with the LLM and the embeddings
    atlas doctor   [atlas.toml]     what can fail on this machine, before a run: Python, extras, sources,
                                    the DOORS database, the compiler commands, variables, workspace and connection
    atlas probe    [atlas.toml]     whether the chat model works as an agent: a tool call, its result,
                                    whether the server uses the reasoning that the agents give back, and
                                    whether it streams
    atlas reference [atlas.toml]    indexes the manuals of [sources.code] reference and the declarations of
                                    its library for search_reference (also done on the first search)
    atlas cache    export FILE [atlas.toml] [--with-judgements]   writes the analyses of sections and the
                                    vectors of this workspace (and the judgements of verify, comply and link)
                                    to FILE, for other workstations
    atlas cache    import FILE [atlas.toml]   adds what FILE has and this workspace lacks; never replaces
    atlas serve    [atlas.toml]     MCP server over stdio, for OpenCode, VS Code...
    atlas init     [atlas.toml]     writes a commented atlas.toml to start from (never overwrites)
                   [--profile ollama|gateway]   for a local model (the default) or the corporate gateway
    atlas --version

    --json         status, sections, graph, trace, matrix, verify, comply, link, coverage and impact
                   print only the result as JSON on stdout (the dictionary of their library
                   function), for scripts; warnings stay on stderr. trace prints Markdown otherwise
"""
from __future__ import annotations

import fnmatch
import json
import logging
import math
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from atlas import plugins
from atlas.config import GRAPH_BUILD_KEYS, Config, ConfigError, load_config
from atlas.db import schema_version
from atlas.pipeline import FatalError, Ledger, RunReport, Runner, Step, Stopping, clock, sha256_file, sha256_text
from atlas.store import Store

__all__ = ["main", "run"]

if TYPE_CHECKING:
    from atlas.docs import DocsOptions

log = logging.getLogger("atlas")


def _logging(cfg: Config) -> None:
    """The log of this action (atlas.logs.setup_logging): stderr and the file of the day."""
    from atlas.llm import usage_label
    from atlas.logs import setup_logging
    setup_logging(cfg, usage_label()[0])


def _context(cfg: Config) -> SimpleNamespace:
    _logging(cfg)
    loaded = plugins.load_plugins(cfg["plugins"]["modules"])
    log.debug("plugins loaded: %s", loaded)
    return SimpleNamespace(cfg=cfg, store=Store(cfg.workspace), disabled=cfg["plugins"]["disabled"],
                           stopping=Stopping(),            # set by the Runner when the run is interrupted
                                                           # or its budget is spent
                           latency=None)                   # seconds per request for the estimates; None = measured


# --------------------------------------------------------------------------
# Estimates: what the pending units of a step will ask the APIs, and how long it takes
# --------------------------------------------------------------------------

def measured_latency(ctx, step: str) -> float | None:
    """Seconds per request of `step` in the last run that made requests in it (api_seconds
    of its profile between its requests), or the --latency given; None if none did."""
    if ctx.latency:
        return ctx.latency
    for r in Ledger(ctx.store.path).runs(20):
        api = r["report"].get("profile", {}).get(step, {}).get("api", {})
        requests = sum(api.get(k, 0) for k in ("chat", "embeddings", "other"))
        if requests and api.get("api_seconds"):
            return api["api_seconds"] / requests
    return None


def request_estimate(ctx, step: str, requests: dict[str, int], most: dict[str, int] | None, service: str,
                     workers: int) -> dict:
    """The Step.estimate of `requests` ({"chat": 540}), or up to `most` of them, to the
    service "llm" or "embeddings": time = max(requests / requests_per_minute, requests x
    latency / requests at a time), with the latency of measured_latency."""
    from atlas.llm import from_config
    conn = from_config(ctx.cfg)
    net = (conn.llm if service == "llm" else conn.embeddings).net
    rpm, at_once = net.requests_per_minute, max(1, min(workers, net.max_concurrent or workers))
    latency = measured_latency(ctx, step)
    most = most or requests
    low, high = sum(requests.values()), sum(most.values())
    what = ", ".join(f"{n} {kind}" for kind, n in requests.items()) + " requests"
    out: dict = {"requests": requests, "most": most}
    if not high:
        return {**out, "seconds": [0, 0], "text": "no requests"}

    def seconds(n: int) -> float | None:
        bounds = ([n / rpm * 60] if rpm else []) + ([n * latency / at_once] if latency is not None else [])
        return max(bounds) if bounds else None

    lo, hi = seconds(low), seconds(high)
    if lo is None:
        return {**out, "text": f"{what}; time unknown: no run has measured the latency yet (--latency SECONDS)"}
    how = (f"at {rpm}/min" if rpm and (latency is None or low / rpm * 60 >= low * latency / at_once)
           else f"with {latency:.2g} s per request, {at_once} at a time")
    return {**out, "seconds": [round(lo, 1), round(hi, 1)], "text": f"{what}, {time_span(lo, hi)} {how}"}


def time_span(low: float, high: float) -> str:
    """'about 45-55 min', 'about 2 h 10 min to 2 h 30 min', 'under 1 min'."""
    a, b = clock(low), clock(high)
    if a == b:
        return a if a.startswith("under") else f"about {a}"
    if high < 90 * 60:
        lo, hi = max(1, round(low / 60)), round(high / 60)
        return f"about {hi} min" if lo == hi else f"about {lo}-{hi} min"
    return f"about {a} to {b}"


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

DOORS_DISPLAY_KEYS = ("attributes", "max_age_days")     # [sources.doors]: change what is shown, read nothing again


def convention(ctx) -> dict:
    """[sources.doors.convention]: how IDs match between DOORS, the code and the documents."""
    return ((ctx.cfg["sources"].get("doors") or {}).get("convention") or {})


def id_regex(ctx) -> str:
    """The expression of an ID inside a tag or a statement (atlas.doors.id_regex), checked."""
    from atlas.doors import id_regex as of
    ids = of(convention(ctx))
    if ids:
        try:
            re.compile(ids)
        except re.error as e:
            raise ConfigError(f"[sources.doors.convention] id_regex (or id_pattern) is not a valid "
                              f"regular expression: {e}") from None
    return ids


def doors_step(ctx) -> Step | None:
    sd = ctx.cfg["sources"].get("doors")
    if not sd or not sd.get("enabled", True):
        return None
    adapter = plugins.get("requirements", sd.get("adapter", "database"), ctx.disabled)(doors_section(ctx.cfg, sd))
    from datetime import datetime
    from atlas.doors import hash_requirement
    started = datetime.now().isoformat(timespec="seconds")      # the run this step belongs to

    def run(ctx, module: str):
        reqs = list(adapter.requirements(module))
        links = list(adapter.links(module))
        hashes = {r.id: hash_requirement(r) for r in reqs}
        previous = ctx.store.requirement_hashes(module)
        ctx.store.replace_module(module, reqs, links, hashes)
        info = adapter.module_info(module)
        ctx.store.put_module_info(module, info.get("exported_at", ""), info.get("source", ""), started)
        changes = {"added": len(set(hashes) - set(previous)), "removed": len(set(previous) - set(hashes)),
                   "changed": sum(1 for i, h in hashes.items() if i in previous and previous[i] != h)}
        log.info("doors %s: %d requirements, %d links (%s)", module, len(reqs), len(links), changes)
        return {"requirements": len(reqs), "links": len(links), **changes}

    def fingerprint(ctx, module: str) -> str:
        # a new export of the same content is read again too, for its date (no model involved)
        exported = adapter.exported_at(module)
        settings = ctx.cfg.fingerprint("sources.doors", exclude=DOORS_DISPLAY_KEYS)
        return f"{adapter.snapshot(module)}|{settings}" + (f"|{exported}" if exported else "")

    return Step(
        name="doors.requirements",
        units=lambda ctx: adapter.modules(),
        fingerprint=fingerprint,
        run=run,
        prune=lambda ctx, m: ctx.store.delete_module(m),
        parallel=False,
    )


def doors_section(cfg: Config, sd: dict) -> dict:
    """[sources.doors] for its adapter, with the paths of its SQLite databases resolved from
    the folder of atlas.toml."""
    sd = dict(sd)
    for table in ("database", "exchange"):
        if isinstance(sd.get(table), dict) and sd[table].get("path"):
            sd[table] = {**sd[table], "path": str(cfg.resolve(sd[table]["path"]))}
    return sd


def code_files(cfg: Config, sc: dict, extensions, warn: bool = False) -> list[str]:
    """The files of the code roots with these extensions, without the excluded ones, named
    from the folder of atlas.toml (Config.relative); `warn`: a warning for a root that is not
    there."""
    exclude, out = sc.get("exclude", []), []
    for root in sc.get("roots", []):
        base = cfg.resolve(root)
        if not base.is_dir():
            if warn:
                log.warning("[sources.code] roots: %s does not exist", root)
            continue
        for p in sorted(base.rglob("*")):
            f = cfg.relative(p)
            if p.is_file() and p.suffix.lower() in extensions and \
                    not any(fnmatch.fnmatch(f.replace("\\", "/"), pat) for pat in exclude):
                out.append(f)
    return out


def code_step(ctx) -> Step | None:
    from atlas.code.gpr import code_config
    sc = ctx.cfg["sources"].get("code")
    if not sc or not sc.get("enabled", True):
        return None
    sc = code_config(ctx.cfg)          # with a GPR project: its source folders and object folders
    ids = id_regex(ctx)                                          # the IDs inside a tag
    analyzers = {}
    resolved = {**sc, "roots": [str(ctx.cfg.resolve(r)) for r in sc.get("roots", [])]}
    for lang in sc.get("languages", ["ada"]):
        try:
            analyzers[lang] = plugins.get("code_analyzer", lang, ctx.disabled)({**resolved, "id_regex": ids} if ids else resolved)
        except LookupError as e:
            raise ConfigError(str(e)) from None
    by_ext = {ext: a for a in analyzers.values() for ext in a.extensions}

    def files(ctx) -> list[str]:
        """The code files, named from the folder of atlas.toml (Config.relative)."""
        return code_files(ctx.cfg, sc, by_ext, warn=True)

    def run(ctx, f: str):
        a = by_ext[Path(f).suffix.lower()]
        path = ctx.cfg.resolve(f)
        unit = a.analyze_file(path, path.read_bytes())
        unit.file = f                   # the name of the unit, not where this process found it
        ctx.store.put_code_unit(unit)
        return {"unit": unit.unit, "subprograms": len(unit.subprograms)}

    return Step(
        name="code.parse",
        units=files,
        fingerprint=lambda ctx, f: f"{sha256_file(ctx.cfg.resolve(f))}|{by_ext[Path(f).suffix.lower()].version()}|"
                                   f"{ctx.cfg.fingerprint('sources.code')}" + (f"|{ids}" if ids else ""),
        run=run,
        prune=lambda ctx, f: ctx.store.delete_code_unit(f),
    )


class AnalysisIncomplete(RuntimeError):
    """Some sections of a document (or its overview) could not be analysed. The output is
    written; the unit stays in error so that the next run retries them."""


DOCS_VERSION = "docs-2"     # change it when the processing of documents changes
GRAPH_ONLY_KEYS = ("authority",)    # keys of [[sources.docs]] that only the graph uses


def doc_sources(ctx) -> list[tuple[dict, "DocsOptions"]]:
    """The enabled [[sources.docs]] with their validated options."""
    from atlas.docs import DocsOptions
    out, names = [], set()
    for d in ctx.cfg["sources"].get("docs", []):
        if not d.get("enabled", True):
            continue
        if d["name"] in names:
            raise ConfigError(f"[sources.docs] the name '{d['name']}' is repeated")
        if d.get("authority", 0) < 0:
            raise ConfigError(f"[sources.docs] '{d['name']}': authority must be 1 (normative), 2... or 0 (none)")
        names.add(d["name"])
        try:
            out.append((d, DocsOptions.from_source(d)))
        except ValueError as e:
            raise ConfigError(f"[sources.docs] '{d['name']}': {e}") from None
    return out


def doc_files(ctx, source: dict) -> list[Path]:
    """Files of a docs source: its paths (files, or folders searched recursively with its
    patterns, from the folder of atlas.toml), without anything inside the workspace."""
    from atlas.docs import extractor_for
    patterns = source.get("patterns") or [
        f"*{ext}" for name in plugins.available("extractor") if f"extractor:{name}" not in ctx.disabled
        for ext in plugins.get("extractor", name).extensions]
    found: set[Path] = set()
    for root in source["paths"]:
        p = ctx.cfg.resolve(root)
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
    from dataclasses import replace
    ids = id_regex(ctx)                # the IDs of the requirements that a regex analysis finds
    sources = [(d, replace(o, id_regex=ids) if ids and o.analysis == "regex" else o) for d, o in doc_sources(ctx)]
    if not sources:
        return None
    from atlas.cache import docs_cache, shared_cache
    from atlas.docs import extractor_for, pending_requests, process_document
    from atlas.docs.analysis import PROMPT_VERSION
    from atlas.llm import JsonLlm, from_config
    from atlas.outbound import record_sent
    by_name = {d["name"]: (d, options) for d, options in sources}
    root = ctx.cfg.workspace / "docs"
    service = from_config(ctx.cfg).llm
    model = service.model
    workers = step_workers(ctx, "docs")
    analyses = shared_cache(ctx.cfg)        # read-only: [project] shared_cache
    concurrency = service.concurrency or workers     # requests to the model in flight, of all the documents
    shared: dict = {}
    lock = threading.Lock()             # documents are processed in parallel
    reserved: dict[str, str] = {}       # output folder -> unit, for the documents of this run

    def llm() -> JsonLlm:
        # only when some source uses it, and only once. It is shared by the documents
        # processed at the same time: `concurrency` sections of all of them at once.
        with lock:
            if "llm" not in shared:
                shared["llm"] = JsonLlm(service, concurrency=concurrency)
        return shared["llm"]

    def units(ctx) -> list[str]:          # <source>:<file named from the folder of atlas.toml>
        return [f"{d['name']}:{ctx.cfg.relative(f)}" for d, _ in sources for f in doc_files(ctx, d)]

    def fingerprint(ctx, unit: str) -> str:
        # the extractor too: another one gives another text (and other sections)
        name, f = unit.split(":", 1)
        d, options = by_name[name]
        d = {k: v for k, v in d.items() if k not in GRAPH_ONLY_KEYS}
        f = ctx.cfg.resolve(f)
        return (f"{sha256_file(f)}|{sha256_text(json.dumps(d, sort_keys=True))[:16]}|{DOCS_VERSION}|"
                f"{extractor_for(f, ctx.disabled).name}|"
                + (f"{model}|{PROMPT_VERSION}" if options.analysis == "llm" else "")
                + (f"|{options.id_regex}" if options.id_regex else ""))

    def out_dir(ctx, unit: str) -> Path:
        """<workspace>/docs/<source>/<name>_<extension>, unless another file already has it
        (or is being processed into it at the same time)."""
        name, f = unit.split(":", 1)
        known = ctx.store.document(unit)
        if known:
            return Path(known["out_dir"])
        out = root / name / f"{Path(f).stem}_{Path(f).suffix.lstrip('.').lower()}"
        with lock:
            if (ctx.store.document_owning(str(out)) or reserved.get(str(out), unit)) != unit:
                out = out.with_name(f"{out.name}-{sha256_text(unit)[:8]}")
            reserved[str(out)] = unit
        return out

    def run(ctx, unit: str):
        name, f = unit.split(":", 1)
        d, options = by_name[name]
        out = out_dir(ctx, unit)
        data = process_document(ctx.cfg.resolve(f), out, options, llm=llm() if options.analysis == "llm" else None,
                                cache_dir=docs_cache(ctx.cfg), workers=concurrency, disabled=ctx.disabled,
                                stop=ctx.stopping, shared_cache=analyses,
                                ocr_dir=root / ".ocr")
        for kind, n in data["sent"].items():
            record_sent(ctx.store.path, model, {f"docs:{name}": n}, kind)
        summary = {"sections": len(data["sections"]), "requirements": len(data["requirements"]),
                   "errors": data["errors"], "structure": data["structure_info"]["source"],
                   **{k: data["structure_info"][k] for k in ("pages_without_text", "ocr", "tables")
                      if data["structure_info"].get(k)}}
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

    def estimate(ctx, units: list[str]) -> dict:
        # the sections that are not in the cache, +10 % for those whose answer comes cut and
        # are split, and the overview of each document whose sections or overview are missing
        sections = to_analyse = overviews = unreadable = 0
        for unit in units:
            name, f = unit.split(":", 1)
            d, options = by_name[name]
            if options.analysis != "llm":
                continue
            try:
                r = pending_requests(ctx.cfg.resolve(f), options, model, root / ".cache", ctx.disabled)
            except Exception as e:
                log.warning("%s: cannot be estimated: %s", f, e)
                unreadable += 1
                continue
            sections, to_analyse, overviews = sections + r["sections"], to_analyse + r["to_analyse"], \
                overviews + r["overview"]
        est = request_estimate(ctx, "docs.process", {"chat": to_analyse + overviews},
                               {"chat": math.ceil(to_analyse * 1.1) + overviews}, "llm", workers)
        return {**est, "sections": sections, "to_analyse": to_analyse, "overviews": overviews,
                **({"unreadable": unreadable} if unreadable else {})}

    # `workers` documents in parallel, and the sections of all of them `concurrency` at a time (llm()):
    # the extraction of a PDF goes on while the model answers about the sections of others
    return Step(name="docs.process", units=units, fingerprint=fingerprint, run=run, prune=prune,
                estimate=estimate)


class IndexIncomplete(RuntimeError):
    """Some records of a document got no vector. The rest is indexed; the unit stays in
    error so that the next run embeds what is missing."""


INDEX_VERSION = "index-3"   # change it when the records or the index tables change (2: meta; 3: tables)


def index_fingerprint(ctx) -> str:
    """What the records of the index depend on in [index]: not fts_tokenizer, since the
    full-text table is rebuilt from the records when it changes (IndexStore)."""
    return ctx.cfg.fingerprint("index", exclude=("fts_tokenizer",))


def embed_settings(ctx) -> str:
    """What the vectors of the index depend on: the embeddings model and its effective
    prefixes (changing the family reindexes)."""
    from atlas.index.embeddings import prefixes
    from atlas.llm import from_config
    emb = from_config(ctx.cfg).embeddings
    return "|".join((emb.model, *prefixes(emb)))


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


def embeddings_estimate(ctx, step: str, index, texts: list, workers: int) -> dict:
    """The Step.estimate of an index step: the batches of the texts that have no vector
    in the cache (embed_missing without sending them)."""
    res = index.embed_missing(texts, workers, dry_run=True)
    return {**request_estimate(ctx, step, {"embeddings": res["batches"]}, None, "embeddings", workers),
            "texts": res["texts"], "cached": res["cached"]}


def doors_attributes(cfg: Config) -> list[str]:
    """[sources.doors] attributes: the DOORS attributes atlas shows and filters by."""
    from atlas.index import ATTRIBUTES
    attributes = (cfg["sources"].get("doors") or {}).get("attributes")
    if attributes is None:
        return list(ATTRIBUTES)
    bad = [a for a in attributes if not isinstance(a, str) or not a.strip() or "=" in a or ";" in a]
    if bad:
        raise ConfigError(f"[sources.doors] attributes: {bad} are not attribute names")
    return [a.strip() for a in attributes]


def index_step(ctx) -> Step:
    """One unit per processed document (docs.process): its structure.json is indexed. The
    texts of all the documents to index are embedded first, together, in full batches."""
    from atlas.index import Index, build_records
    from atlas.outbound import LocalOnly, source_label
    options = index_options(ctx)
    workers = step_workers(ctx, "index")
    embed_model = embed_settings(ctx)
    local = LocalOnly.from_config(ctx.cfg)
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
        return (f"{sha256_file(structure(unit))}|{index_fingerprint(ctx)}|{embed_model}|{INDEX_VERSION}"
                + ("|local" if local.index_doc(unit) else ""))

    def run(ctx, unit: str):
        d = ctx.store.document(unit)
        name = Path(d["path"]).name             # what citations show, unless another document has it
        if any(x["name"] == name and x["doc"] != unit for x in index().documents()):
            name = d["path"]
        res = index().add_document(json.loads(structure(unit).read_text(encoding="utf-8")), doc=unit,
                                   name=name, out_dir=d["out_dir"], local=local.index_doc(unit), **options)
        if res["missing"]:
            raise IndexIncomplete(f"{res['missing']} records got no vector ({len(res['failed_batches'])} "
                                  f"failed batches); they are embedded on the next run")
        return {"records": res["records"], "kinds": res["kinds"]}

    def texts(units: list[str], labels: dict | None = None) -> list[tuple[str, str]]:
        """What embedding the documents sends (not those of a local-only source); `labels`
        gets the source of each text, for the record of what was sent."""
        out = []
        for unit in units:
            if local.index_doc(unit):
                continue
            data = json.loads(structure(unit).read_text(encoding="utf-8"))
            new = [(r["embed_text"], data["title"]) for r in build_records(
                data, options["chunk_chars"], options["chunk_overlap"], options["kinds"])]
            if labels is not None:
                labels.update((t, source_label(unit)) for t, _ in new)
            out += new
        return out

    def prepare(ctx, units: list[str]) -> None:
        labels: dict[str, str] = {}
        index().embed_missing(texts(units, labels), workers, sources=labels)

    return Step(name="index.documents", units=units, fingerprint=fingerprint, run=run,
                prune=lambda ctx, unit: index().remove_document(unit), prepare=prepare,
                estimate=lambda ctx, units: embeddings_estimate(ctx, "index.documents", index(), texts(units), workers),
                depends_on=["docs.process"])


def index_requirements_step(ctx) -> Step:
    """One unit per DOORS module read by doors.requirements: its requirements are indexed
    as one more document ("doors:<module>"), so that search finds them too. Like
    index.documents, the texts of all the modules to index are embedded first, together."""
    from atlas.index import DOORS_PREFIX, Index, module_title, requirement_records
    from atlas.outbound import LocalOnly
    local = LocalOnly.from_config(ctx.cfg)
    kinds = index_options(ctx)["kinds"]
    attributes = doors_attributes(ctx.cfg)
    workers = step_workers(ctx, "index")
    embed_model = embed_settings(ctx)
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
        rows = json.dumps(ctx.store.requirements(module, in_module_order=True), sort_keys=True, default=str)
        return (f"{sha256_text(rows)}|{index_fingerprint(ctx)}|{attributes}|{embed_model}|{INDEX_VERSION}"
                + ("|local" if local.module(module) else ""))

    def run(ctx, module: str):
        res = index().add_module(module, ctx.store.requirements(module, in_module_order=True), kinds, attributes,
                                 local=local.module(module))
        if res["missing"]:
            raise IndexIncomplete(f"{res['missing']} records got no vector ({len(res['failed_batches'])} "
                                  f"failed batches); they are embedded on the next run")
        return {"records": res["records"], "kinds": res["kinds"]}

    def texts(modules: list[str]) -> list[tuple[str, str]]:
        return [(r["embed_text"], module_title(m)) for m in modules if not local.module(m)
                for r in requirement_records(m, ctx.store.requirements(m, in_module_order=True), kinds, attributes)]

    def prepare(ctx, modules: list[str]) -> None:
        sent = texts(modules)
        index().embed_missing(sent, workers, sources={t: DOORS_PREFIX + m for m in modules if not local.module(m)
                                                      for t, title in sent if title == module_title(m)})

    return Step(name="index.requirements", units=units, fingerprint=fingerprint, run=run,
                prune=lambda ctx, module: index().remove_document(DOORS_PREFIX + module),
                prepare=prepare,
                estimate=lambda ctx, modules: embeddings_estimate(ctx, "index.requirements", index(), texts(modules),
                                                                  workers),
                depends_on=["doors.requirements"])


def index_code_step(ctx) -> Step | None:
    """One unit per code unit read by code.parse (an Ada package: its spec and its body):
    each subprogram, test, type and variable is a code_doc record, with its comment, its
    requirements and its code, so that search finds code too. Like index.documents, the
    texts of all the units to index are embedded first, together."""
    sc = ctx.cfg["sources"].get("code")
    options = index_options(ctx)
    if not sc or not sc.get("enabled", True) or "code_doc" not in options["kinds"]:
        return None
    from atlas.graph.build import is_test_file
    from atlas.index import CODE_PREFIX, Index, code_records, code_title
    local = sc.get("model") == "none"           # [sources.code] model: nothing of the code is embedded
    workers = step_workers(ctx, "index")
    embed_model = embed_settings(ctx)
    test_patterns = sc.get("test_patterns", [])
    shared: dict = {}
    lock = threading.Lock()

    def index() -> Index:
        with lock:
            if "index" not in shared:
                shared["index"] = Index.from_config(ctx.cfg)
        return shared["index"]

    def parts(unit: str) -> list[dict]:
        return shared["units"][unit]

    def sources(unit: str) -> dict[str, str]:
        return {p["file"]: ctx.cfg.resolve(p["file"]).read_text(encoding="utf-8", errors="replace")
                for p in parts(unit)}

    def records(unit: str) -> list[dict]:
        tests = [p["file"] for p in parts(unit) if is_test_file(p["file"], test_patterns)]
        return code_records(unit, parts(unit), sources(unit), tests, options["chunk_chars"], options["kinds"])

    def units(ctx) -> list[str]:
        grouped: dict[str, list[dict]] = {}
        for u in ctx.store.code_units():
            grouped.setdefault(u["unit"], []).append(u)
        shared["units"] = grouped
        return sorted(grouped)

    def fingerprint(ctx, unit: str) -> str:
        files = [[p, sha256_file(ctx.cfg.resolve(p["file"])) if ctx.cfg.resolve(p["file"]).exists() else ""]
                 for p in parts(unit)]
        return (f"{sha256_text(json.dumps(files, sort_keys=True))}|{test_patterns}|{index_fingerprint(ctx)}|"
                f"{embed_model}|{INDEX_VERSION}" + ("|local" if local else ""))

    def run(ctx, unit: str):
        tests = [p["file"] for p in parts(unit) if is_test_file(p["file"], test_patterns)]
        res = index().add_code(unit, parts(unit), sources(unit), tests, options["chunk_chars"], options["kinds"],
                               local=local)
        if res["missing"]:
            raise IndexIncomplete(f"{res['missing']} records got no vector ({len(res['failed_batches'])} "
                                  f"failed batches); they are embedded on the next run")
        return {"records": res["records"], "kinds": res["kinds"]}

    def texts(pending: list[str]) -> list[tuple[str, str]]:
        return [] if local else [(r["embed_text"], code_title(unit)) for unit in pending for r in records(unit)]

    return Step(name="index.code", units=units, fingerprint=fingerprint, run=run,
                prune=lambda ctx, unit: index().remove_document(CODE_PREFIX + unit),
                prepare=lambda ctx, pending: index().embed_missing(texts(pending), workers,
                                                                   sources={t: "code" for t, _ in texts(pending)}),
                estimate=lambda ctx, pending: embeddings_estimate(ctx, "index.code", index(), texts(pending), workers),
                depends_on=["code.parse"])


def code_revision(roots: list[str]) -> str:
    """`git rev-parse HEAD` in the first code root; "" without git or outside a repository."""
    import subprocess
    if not roots:
        return ""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=roots[0], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        log.info("no code revision: %s", e)
        return ""
    if out.returncode:
        log.info("no code revision: %s", out.stderr.strip()[:200])
    return out.stdout.strip() if out.returncode == 0 else ""


def _exports(store: Store) -> dict[str, dict]:
    """When and from where each DOORS module was exported, without the run that read it."""
    return {m: {"exported_at": i["exported_at"], "source": i["source"]} for m, i in store.modules_info().items()}


GRAPH_VERSION = "graph-4"   # change it when the building of the graph changes (3: overloads, 4: uses, aliases)
GRAPH_EXPORTS = ("graphml", "json")


def graph_step(ctx) -> Step:
    """One unit per part of the graph (its origin): "code" (code.parse), "doors"
    (doors.requirements) and "docs" (docs.process). Each one is rebuilt whole (seconds)
    when what it is built from changes; then the whole graph is exported."""
    from atlas.doors import canonical_of, normalize_id
    from atlas.graph import (GraphStore, build_code_graph, build_docs_graph, build_doors_graph, code_names,
                             document_names, export_graph, find_communities)
    from atlas.graph.sources import graph_convention
    exports = ctx.cfg["graph"]["exports"]
    unknown = sorted(set(exports) - set(GRAPH_EXPORTS))
    if unknown:
        raise ConfigError(f"[graph] unknown exports {unknown} (valid: {', '.join(GRAPH_EXPORTS)})")
    folder = ctx.cfg.workspace / "graph"
    test_patterns = (ctx.cfg["sources"].get("code") or {}).get("test_patterns", [])
    conv = convention(ctx)
    id_normalize = conv.get("id_normalize", "upper")
    try:                               # code and documents meet DOORS in the canonical form; DOORS keeps its IDs
        canonical = canonical_of(conv)
    except ValueError as e:
        raise ConfigError(f"[sources.doors.convention] {e}") from None
    normalize = lambda rid: normalize_id(rid, id_normalize, canonical)
    ids = id_regex(ctx)
    # only what changes the graph built: the matrix formats ([graph] matrix_formats...) do not
    built = {k: v for k, v in ctx.cfg["graph"].items() if k in GRAPH_BUILD_KEYS}
    settings = (f"{sha256_text(json.dumps(built, sort_keys=True, ensure_ascii=False, default=str))[:16]}|"
                f"{test_patterns}|{id_normalize}|{json.dumps(conv.get('id_canonical') or {}, sort_keys=True)}|"
                f"{ids}|{GRAPH_VERSION}")

    authority = {d["name"]: d.get("authority", 0) for d in ctx.cfg["sources"].get("docs", [])}

    def documents(ctx) -> list[dict]:
        docs = [d for d in ctx.store.documents() if (Path(d["out_dir"]) / "structure.json").exists()]
        names = document_names(docs)
        return [{**d, "name": names[d["unit"]], "authority": authority.get(d["source"], 0),
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
            data = [ctx.store.requirements(), ctx.store.links(), graph_convention(conv), _exports(ctx.store)]
        else:
            docs = [d for d in ctx.store.documents() if (Path(d["out_dir"]) / "structure.json").exists()]
            data = [[d["unit"], d["path"], sha256_file(Path(d["out_dir"]) / "structure.json"),
                     authority.get(d["source"], 0)] for d in docs]
            data.append(sorted(code_names(ctx.store.code_units()).items()))    # what the sections can mention
        return f"{sha256_text(json.dumps(data, sort_keys=True, default=str))}|{settings}"

    def build(ctx, unit: str):
        if unit == "code":
            G, stats = build_code_graph(ctx.store.code_units(), test_patterns, normalize)
            if ctx.cfg["graph"]["code_revision"]:
                revision = code_revision([str(ctx.cfg.resolve(r)) for r in (ctx.cfg["sources"].get("code") or {}).get("roots", [])])
                if revision:
                    stats["code_revision"] = revision
            if ctx.cfg["graph"]["communities"]:          # in the node, for the exports (Gephi colours)
                groups = find_communities(G)
                for n, group in groups.items():
                    G.nodes[n]["community"] = group
                stats["communities"] = len(set(groups.values()))
            return G, stats
        if unit == "doors":
            return build_doors_graph(ctx.store.requirements(), ctx.store.links(), conv, modules=_exports(ctx.store))
        return build_docs_graph(documents(ctx), code_names(ctx.store.code_units()), normalize,
                                re.compile(ids) if ids else None)

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
        for x in stats.get("superseded", []):
            log.warning("%s (%s v%s) is older than %s (v%s): is it superseded?", x["document"], x["doc_id"],
                        x["version"], x["by"], x["by_version"])
        return {k: v for k, v in stats.items() if not k.startswith("top_")}

    def prune(ctx, unit: str) -> None:
        store = GraphStore(ctx.store.path)
        store.delete(unit)
        export(store)

    return Step(name="graph.build", units=units, fingerprint=fingerprint, run=run, prune=prune,
                depends_on=["code.parse", "doors.requirements", "docs.process"], parallel=False)


STEPS = ("doors", "code", "docs", "graph", "index")
PARALLEL_STEPS = ("code", "docs", "index")      # the others process their units one by one


def step_workers(ctx, name: str) -> int:
    """Units of the step `name` processed at the same time: [pipeline.step_workers] or, if
    it does not say, [pipeline] workers. In docs they are also the sections that are
    analysed at the same time, unless [llm] concurrency says otherwise."""
    per_step = ctx.cfg["pipeline"]["step_workers"]
    for key, n in per_step.items():
        if key not in STEPS:
            raise ConfigError(f"[pipeline.step_workers] unknown step '{key}' (valid: {', '.join(PARALLEL_STEPS)})")
        if key not in PARALLEL_STEPS:
            raise ConfigError(f"[pipeline.step_workers] '{key}' processes its units one by one "
                              f"(valid: {', '.join(PARALLEL_STEPS)})")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ConfigError(f"[pipeline.step_workers] '{key}' must be a positive integer")
    return per_step.get(name, ctx.cfg["pipeline"]["workers"])


def steps(ctx) -> list[Step]:
    wanted = ctx.cfg["pipeline"]["steps"]
    builders = {"doors": [doors_step], "code": [code_step], "docs": [docs_step], "graph": [graph_step],
                "index": [index_step, index_requirements_step, index_code_step]}
    out = []
    for name in wanted:
        if name not in builders:
            raise ConfigError(f"[pipeline] unknown step '{name}'")
        for s in (build(ctx) for build in builders[name]):
            if s:
                s.workers = step_workers(ctx, name) if name in PARALLEL_STEPS else None
                out.append(s)
    return out


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def _print_json(data) -> None:
    """--json: the result alone on stdout."""
    print(json.dumps(data, ensure_ascii=False, indent=1, default=str))


def _json_result(res: dict) -> int:
    _print_json(res)
    return 1 if "error" in res else 0


def _runner(ctx, **options) -> Runner:
    from atlas import __version__
    from atlas.llm import usage
    pc = ctx.cfg["pipeline"]
    return Runner(Ledger(ctx.store.path), steps(ctx), context=ctx, workers=pc["workers"],
                  on_error=pc["on_error"], max_attempts=pc["max_attempts"],
                  retry_quarantined=pc["retry_quarantined"], prune_missing=pc["prune_missing"],
                  meters=usage, version=__version__, stopping=ctx.stopping, **options)


def print_estimate(rep: RunReport, indent: str = "") -> None:
    """One line per step of a dry run (Runner(dry_run=True)), and the total."""
    totals: dict[str, int] = {}
    low = high = 0.0
    known = True
    for step, n in rep.pending.items():
        est = rep.estimate.get(step, {})
        after = ", ".join(est.get("after", []))
        if n:
            line = f"{n} unit{'s' if n > 1 else ''}" + (f", {est['text']}" if est.get("text") else "") + (f"; more after {after}" if after else "")
        else:
            line = f"after {after}" if after else "up to date"
        print(f"{indent}{step}: {line}")
        for kind, k in est.get("requests", {}).items():
            totals[kind] = totals.get(kind, 0) + k
        if est.get("requests") and sum(est["requests"].values()):
            if "seconds" in est:
                low, high = low + est["seconds"][0], high + est["seconds"][1]
            else:
                known = False
    if sum(totals.values()):
        print(f"{indent}total: " + ", ".join(f"{n} {kind}" for kind, n in totals.items() if n) + " requests"
              + (f", {time_span(low, high)}" if known else "; time unknown (--latency SECONDS)"))


def estimate(cfg: Config, latency: float = 0.0) -> int:
    """What `atlas run` would do now, without doing it: the units to process of each step,
    the requests to the APIs and how long they take."""
    ctx = _context(cfg)
    ctx.latency = latency or None
    print_estimate(_runner(ctx, dry_run=True).run())
    return 0


def deadline(until: str = "", minutes: int = 0, now: float | None = None) -> float | None:
    """The instant (time.time()) after which `atlas run` starts no more units: the earliest
    of `until` ("HH:MM", today or, if that time has passed, tomorrow) and `minutes` from
    now; None without either."""
    from datetime import datetime, timedelta
    now = time.time() if now is None else now
    out = [now + minutes * 60] if minutes > 0 else []
    if until:
        try:
            hh, mm = (int(x) for x in until.split(":"))
            at = datetime.fromtimestamp(now).replace(hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            raise ConfigError(f"--until must be a time HH:MM, not '{until}'") from None
        if at.timestamp() <= now:
            at += timedelta(days=1)
        out.append(at.timestamp())
    return min(out) if out else None


def run(cfg: Config, until: str = "", max_minutes: int = 0, max_requests: int = 0) -> int:
    """Exit code: 0 all done; 1 some unit failed (retried on the next run); 2 a fatal error
    stopped it; 3 the budget (--until, --max-minutes, max_requests) stopped it. With 2 and 3
    the state is saved and running it again goes on."""
    pc = cfg["pipeline"]
    for key, value in (("max_minutes", max_minutes or pc["max_minutes"]),
                       ("max_requests", max_requests or pc["max_requests"])):
        if value < 0:
            raise ConfigError(f"[pipeline] {key} must be 0 (no limit) or more, not {value}")
    from atlas.llm import set_usage_label
    from atlas.outbound import sent_totals
    ctx = _context(cfg)
    before = sent_totals(ctx.store.path)
    try:
        rep = _runner(ctx, deadline=deadline(until, max_minutes or pc["max_minutes"]),
                      max_requests=max_requests or pc["max_requests"],
                      progress=lambda line: print(line, file=sys.stderr, flush=True),
                      on_step=lambda step: set_usage_label("run", step)).run()
    finally:
        set_usage_label("run")                   # the steps are over
    sent: dict = {}                     # what this run sent to each model, by source
    for (model, source, kind), n in sent_totals(ctx.store.path).items():
        if n > before.get((model, source, kind), 0):
            sent.setdefault(model, {}).setdefault(source, {})[kind] = n - before.get((model, source, kind), 0)
    print(json.dumps({**rep.as_dict(), "sent": sent}, ensure_ascii=False, indent=1))
    from atlas.doors import warn_stale_exports
    warn_stale_exports(cfg["sources"].get("doors"), ctx.store.modules_info())
    if rep.fatal:
        print("Stopped by a fatal error; the state is saved: run `atlas run` again.", file=sys.stderr)
        return 2
    if rep.stopped_by == "budget":
        print("budget reached; the state is saved: run `atlas run` again", file=sys.stderr)
        return 3
    return 1 if rep.errors or rep.quarantined else 0


def _api_text(api: dict) -> str:
    """'540 chat, 189 embeddings, 3 rejected, 12.0 s waiting, 812000 prompt / 95000 completion
    tokens, 40000 embedding tokens, $0.41' from the `api` of a profile (or a usage_since)."""
    parts = [f"{api[k]:g} {k}" for k in ("chat", "embeddings", "other", "rejected", "retried") if api.get(k)]
    if api.get("wait_seconds", 0) >= 0.1:
        parts.append(f"{api['wait_seconds']:.1f} s waiting")
    tokens = _tokens_text(api)
    return ", ".join(parts + ([tokens] if tokens else []))


def _tokens_text(api: dict) -> str:
    """'812000 prompt / 95000 completion tokens, 40000 embedding tokens, $0.41'."""
    parts = []
    if api.get("prompt_tokens") or api.get("completion_tokens"):
        parts.append(f"{api.get('prompt_tokens', 0):g} prompt / {api.get('completion_tokens', 0):g} completion tokens"
                     + (f" ({api['reasoning_tokens']:g} reasoning)" if api.get("reasoning_tokens") else ""))
    if api.get("embedding_tokens"):
        parts.append(f"{api['embedding_tokens']:g} embedding tokens")
    if api.get("cost"):
        parts.append(f"${api['cost']:.4f}")
    return ", ".join(parts)


def _span(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 10 else f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.0f} min"


def pace_lines(profile: dict, rpm: dict[str, int]) -> list[str]:
    """What a step asked of each endpoint and how full it kept the quota, with Little's law:
    'chat: 540 in 31 min = 17/min of 28; latency 21 s; 6.0 in flight; 8 would fill the quota'.
    The requests in flight on average are the seconds the server spent on them over the
    seconds of the step; to fill `rpm` requests per minute of `latency` seconds each, about
    rpm x latency / 60 have to be in flight. `rpm` by endpoint, from [network]."""
    import math
    api, seconds, out = profile.get("api") or {}, profile.get("seconds", 0), []
    for endpoint in ("chat", "embeddings"):
        n = api.get(endpoint, 0)
        if not n or seconds <= 0:
            continue
        busy = api.get(f"{endpoint}_seconds")
        if busy is None:                    # a run of atlas 0.2: the seconds of all the endpoints
            total = sum(api.get(k, 0) for k in ("chat", "embeddings", "other"))
            busy = api.get("api_seconds", 0) * n / total if total else 0
        latency, limit = busy / n, rpm.get(endpoint, 0)
        line = f"{endpoint}: {n:g} in {_span(seconds)} = {n * 60 / seconds:.0f}/min" + (f" of {limit}" if limit else "")
        line += f"; latency {latency:.2f} s" if latency < 1 else f"; latency {latency:.1f} s" if latency < 10 \
            else f"; latency {latency:.0f} s"
        line += f"; {busy / seconds:.1f} in flight"
        if limit:
            line += f"; {max(1, math.ceil(limit * latency / 60))} would fill the quota"
        out.append(line)
    return out


def print_profile(run: dict, rpm: dict[str, int] | None = None) -> None:
    """The time profile of a run (Ledger.runs): one line per step and, under it, the pace of
    the requests to each endpoint (pace_lines, with the limits `rpm` of [network])."""
    rep = run["report"]
    print(f"last run: {run['started']}, atlas {run['version'] or '?'}, {run['seconds']:.1f} s"
          + (f", stopped: {rep['fatal']}" if rep.get("fatal") else "")
          + (", stopped by the budget: run `atlas run` again to go on" if rep.get("stopped_by") == "budget" else ""))
    print(f"  {'step':20s} {'seconds':>8s} {'scan':>6s} {'units':>6s} {'ran':>6s}  slowest unit; API")
    for step, p in rep.get("profile", {}).items():
        slowest = p.get("slowest", [])
        notes = [f"{Path(slowest[0]['unit'].split(':', 1)[-1]).name} ({slowest[0]['seconds']:.2f} s)"] if slowest else []
        notes += [_api_text(p["api"])] if p.get("api") and _api_text(p["api"]) else []
        print(f"  {step:20s} {p['seconds']:8.2f} {p['scan_seconds']:6.2f} {p['units']:6d} {p['ran']:6d}  "
              + "; ".join(notes))
        for line in pace_lines(p, rpm or {}):
            print(f"  {'':20s} {line}")


def print_sent(cfg: Config, db: Path) -> None:
    """What has gone to each model, by source (the record of atlas.outbound), and the
    sources that never go."""
    from atlas.outbound import LocalOnly, sent_summary
    rows = sent_summary(db)
    local = LocalOnly.from_config(cfg).describe()
    print("sent to the models" + (f" (never: {', '.join(local)}, model = \"none\")" if local else "") + ":"
          + ("" if rows else " nothing recorded"))
    for model in dict.fromkeys(r["model"] for r in rows):
        print(f"  {model}")
        for source in dict.fromkeys(r["source"] for r in rows if r["model"] == model):
            mine = [r for r in rows if r["model"] == model and r["source"] == source]
            print(f"    {source:28s} " + ", ".join(f"{r['count']} {r['kind']}" for r in mine)
                  + f"; last {max(r['last'] for r in mine).replace('T', ' ')}")


def status(cfg: Config, as_json: bool = False) -> int:
    from atlas.graph import GraphStore
    from atlas.index import IndexStore
    from atlas.outbound import sent_summary
    ctx = _context(cfg)
    ledger = Ledger(ctx.store.path)
    if as_json:
        _print_json({"workspace": str(cfg.workspace), "data": ctx.store.counts(),
                     "graph": GraphStore(ctx.store.path).counts(), "index": IndexStore(ctx.store.path).counts(),
                     "ledger": ledger.summary(), "pending": ledger.pending(), "runs": ledger.runs(6),
                     "doors_exports": ctx.store.modules_info(), "sent": sent_summary(ctx.store.path),
                     "schema_version": schema_version(ctx.store.db)})
        return 0
    from atlas.logs import log_file
    runs = ledger.runs(6)
    print(f"workspace: {cfg.workspace}")
    print(f"schema: version {schema_version(ctx.store.db)}; "
          + (f"last run with atlas {runs[0]['version'] or '?'}" if runs else "no runs yet"))
    print(f"log: {log_file(cfg) or 'off ([project] log_dir is empty)'}")
    print("data:", ctx.store.counts())
    exports = ctx.store.modules_info()
    if exports:
        from atlas.doors import describe_exports
        print("DOORS:", describe_exports(exports))
    print("graph:", GraphStore(ctx.store.path).counts())
    print("index:", IndexStore(ctx.store.path, ctx.cfg["index"]["fts_tokenizer"]).counts())
    print("ledger:", json.dumps(ledger.summary(), ensure_ascii=False))
    pending = ledger.pending()
    for p in pending[:30]:
        print(f"  {p['status']:11s} {p['step']:20s} {p['unit'][:60]}  {p['error'] or ''}")
    if len(pending) > 30:
        print(f"  … and {len(pending) - 30} more")
    try:
        rep = _runner(ctx, dry_run=True).run()
        if any(rep.pending.values()):
            print("to do now:")
            print_estimate(rep, "  ")
        else:
            print("to do now: nothing, everything is up to date")
    except Exception as e:                          # e.g. the DOORS database is not reachable
        print(f"to do now: unknown ({e})")
    print_sent(cfg, ctx.store.path)
    if runs:
        from atlas.llm import from_config
        conn = from_config(cfg)
        print_profile(runs[0], {"chat": conn.llm.net.requests_per_minute,
                                "embeddings": conn.embeddings.net.requests_per_minute})
    if len(runs) > 1:
        print("previous runs:")
        for r in runs[1:]:
            api = {}
            for p in r["report"].get("profile", {}).values():
                for k, v in p.get("api", {}).items():
                    api[k] = api.get(k, 0) + v
            print(f"  {r['started']}  atlas {r['version'] or '?':8s} {r['seconds']:8.1f} s  "
                  f"{sum(r['report']['processed'].values())} processed" + (f"; {_api_text(api)}" if _api_text(api) else ""))
    return 0


def sections(cfg: Config, as_json: bool = False) -> int:
    """The sections of every document of [[sources.docs]], without analysing them: to
    tune structure, heading_regex, max_chars and min_chars before a run."""
    from atlas.docs import preview
    ctx = _context(cfg)
    if as_json:
        _print_json({"documents": [{"source": d["name"], "file": str(f), **preview(f, options, ctx.disabled)}
                                   for d, options in doc_sources(ctx) for f in doc_files(ctx, d)]})
        return 0
    for d, options in doc_sources(ctx):
        for f in doc_files(ctx, d):
            p = preview(f, options, ctx.disabled, ocr_dir=cfg.workspace / "docs" / ".ocr")
            print(f"\n[{d['name']}] {f} — '{p['title']}': {p['chars']} chars, {len(p['sections'])} sections")
            if any(p[k] for k in ("doc_id", "version", "date")):
                print("  " + "; ".join(f"{k}: {p[k]}" for k in ("doc_id", "version", "date") if p[k]))
            if p.get("scanned"):
                print(f"  ! {p['scanned']}: `atlas run` leaves it in error; give the source an ocr_command "
                      "or a PDF with text")
            elif p.get("pages_without_text"):
                print(f"  ! {p['pages_without_text']} of {p['pages']} pages have no text (scanned?)")
            print(f"  extractor: {p['extractor']}" + (" (after ocr_command)" if p.get("ocr") else "")
                  + (f"; {p['tables']} tables as Markdown rows" if p.get("tables") else "")
                  + f"; structure: {p['structure']}"
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


def id_shape_text(shape: dict) -> str:
    """'SRS-NNNN' for a shape of atlas.doors.id_shapes; 'other (TBD)' for the IDs without digits."""
    if shape["prefix"] == "other" and not shape["digits"]:
        return f"other ({shape['example']})"
    return f"{shape['prefix']}{shape['separator']}{'N' * shape['digits']}"


def graph(cfg: Config, as_json: bool = False) -> int:
    """The report of the last build of every part of the graph, and of its gaps."""
    from atlas.graph import GraphStore, KnowledgeGraph
    ctx = _context(cfg)
    builds = GraphStore(ctx.store.path).builds()
    if not builds:
        message = "no graph yet: run `atlas run` with \"graph\" in [pipeline] steps"
        if as_json:
            return _json_result({"error": message})
        print(message)
        return 1
    if as_json:
        return _json_result({"builds": builds, "gaps": KnowledgeGraph.from_config(cfg).gaps(),
                             "exported_to": str(cfg.workspace / "graph")})
    s = builds.get("code")
    if s:
        print(f"code graph of {s['built_at'][:19]}: {s['nodes']} nodes, {s['edges']} edges ({s['subprograms']} "
              f"subprograms, {s['tests']} tests, {s['types']} types, {s['variables']} variables, {s['externals']} "
              f"externals, {s['requirements']} requirements) from {s['files']} files ({s['test_files']} of tests)")
        if s.get("subprogram_kinds"):
            print("subprograms and tests by kind: " + ", ".join(f"{n} {k}" for k, n in s["subprogram_kinds"].items())
                  + (f" | overloaded names (one node each): {s['overloaded']}" if s.get("overloaded") else ""))
        c = s["edges_by_kind"]
        print(f"calls: {c['calls']} | type conversions: {c['converts']} | global variables used: {c['uses']} | "
              f"predefined types ignored: {c['predefined']} | ambiguous calls: {s['ambiguous_calls']}"
              + (f" | resolved through aliases: {s['calls_through_aliases']}, through use clauses: "
                 f"{s['calls_through_uses']}" if "calls_through_aliases" in s else ""))
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
    g = KnowledgeGraph.from_config(cfg)
    s = builds.get("doors")
    if s:
        from atlas.doors import describe_exports
        print(f"\nDOORS of {s['built_at'][:19]}: {s['modules']} modules, {s['requirements']} requirements, "
              f"{s['headings']} headings; links: "
              + ", ".join(f"{n} {k}" for k, n in s["edges_by_kind"].items() if k != "contains")
              + (f"; {s['linked_outside']} IDs linked from modules that were not read" if s["linked_outside"] else ""))
        print("DOORS export: " + describe_exports(g.exports()).split(", ", 1)[-1]
              + "".join(f"\n  {m}: {x['exported_at'][:19] or '?'}" + (f" from {x['source']}" if x["source"] else "")
                        for m, x in g.exports().items()))
    s = builds.get("docs")
    if s:
        print(f"\ndocuments of {s['built_at'][:19]}: {s['documents']} documents, {s['sections']} sections, "
              f"{s['requirements']} requirements stated, {s['mentions']} mentions of code elements")
        for x in s.get("superseded", []):
            print(f"  ! {x['document']} ({x['doc_id']} v{x['version']}) is older than {x['by']} "
                  f"(v{x['by_version']}): is it superseded?")
    shapes = {o: b["id_shapes"] for o, b in builds.items() if b.get("id_shapes")}
    if shapes:
        print("\nID shapes as written, by origin (N = a digit; how many times each one is written):")
        for origin, items in shapes.items():
            print(f"  {origin:6s} " + ", ".join(f"{x['count']} {id_shape_text(x)}" for x in items))
    gaps = g.gaps()
    print("\ntraceability gaps (atlas matrix MODULE for the detail):")
    for key, value in gaps.items():
        if isinstance(value, dict):
            print(f"  {value['count']:5d}  {key.replace('_', ' ')}")
    print(f"\nexported to {cfg.workspace / 'graph'}")
    return 0


TRACE_BUDGET = 20000     # tokens of `atlas trace`: more room than the build_context tool, not to cut on screen


def trace(cfg: Config, req_id: str, as_json: bool = False) -> int:
    """Everything about a requirement: in Markdown (build_context, without a model) or, with
    --json, the dictionary of KnowledgeGraph.trace."""
    from atlas.agents.context import build_context
    from atlas.graph import KnowledgeGraph
    _context(cfg)                         # the logging of the configuration
    g = KnowledgeGraph.from_config(cfg)
    res = g.trace(req_id)
    if as_json:
        return _json_result(res)
    if "error" in res:
        print(res["error"] + (f"; similar: {', '.join(res['similar'])}" if res.get("similar") else ""))
        return 1
    print(build_context(g, res["id"], budget_tokens=TRACE_BUDGET)["markdown"].rstrip())
    return 0


def _held(counts: dict) -> str:
    """What a summary says of the requirements that were not sent to the model."""
    n = counts.get("local_only", 0)
    return f"; {n} not sent to the model: local-only sources (model = \"none\")" if n else ""


def matrix(cfg: Config, module: str, as_json: bool = False) -> int:
    """The traceability matrix of a module ("*": every requirement) in <workspace>/graph, in
    the formats of [graph] matrix_formats (atlas.graph.export), and its summary and gaps on
    the screen."""
    from atlas.graph import GraphStore, KnowledgeGraph, write_matrix
    from atlas.graph.export import matrix_header, summary_line
    ctx = _context(cfg)
    module = "" if module == "*" else module
    g = KnowledgeGraph.from_config(cfg)
    res = g.matrix(module, limit=10**9)
    if "error" in res:
        if as_json:
            return _json_result(res)
        print(res["error"] + (f"; modules: {', '.join(res['modules'])}" if res.get("modules") else ""))
        return 1
    gc = cfg["graph"]
    try:
        files = write_matrix(res, cfg.workspace / "graph" / f"matrix-{module or 'all'}", gc["matrix_formats"],
                             gc["csv_delimiter"], builds=GraphStore(ctx.store.path).builds(),
                             modules={row["id"]: g.G.nodes[row["id"]].get("module", "") for row in res["rows"]})
    except ImportError as e:            # "xlsx" without the [xlsx] extra
        sys.exit(str(e))
    except PermissionError as e:        # Windows: the file is open in Excel
        sys.exit(f"cannot write {e.filename}: close it (Excel keeps it locked) and run again")
    if as_json:
        return _json_result(res)
    gaps = res["gaps"]
    for line in matrix_header(res["meta"]):
        print(line.removeprefix("- "))
    print(summary_line(res))
    for key, title in (("without_code", "without code"), ("without_tests", "without tests"),
                       ("without_test_procedure", "without a test procedure")):
        items = gaps[key]["items"]
        print(f"{title} ({gaps[key]['count']}): {', '.join(items[:20])}" + (" …" if len(items) > 20 else ""))
    print(f"written {', '.join(str(f) for f in files)}" if files else "nothing written: [graph] matrix_formats is empty")
    return 0


def verify(cfg: Config, target: str, as_json: bool = False) -> int:
    """Each requirement against the documentation: the inconsistencies on the screen, and the
    report in <workspace>/reports."""
    from atlas.agents.verify import verify_all
    from atlas.index import Index
    ctx = _context(cfg)
    index = Index.from_config(cfg) if ctx.store.documents() else None
    res = verify_all(target, cfg, index=index)
    if as_json:
        return _json_result(res)
    if "error" in res:
        print(res["error"] + (f"; modules: {', '.join(res['modules'])}" if res.get("modules") else ""))
        return 1
    c = res["counts"]
    print(f"{len(res['results'])} requirements: {c['inconsistent']} inconsistent, {c['consistent']} consistent, "
          f"{c['no_documentation']} without documentation ({res['asked']} asked to the model, the rest from the cache)"
          + _held(c))
    for r in res["results"]:
        if r.get("verdict") == "inconsistent":
            print(f"\n{r['id']}: {r.get('summary', '')}")
            for i in r["issues"]:
                print(f"  - {i['type']}: DOORS «{i['official']}» / «{i['document']}» ({i['source']})")
    print(f"\nwritten {res['files'][0]} and {res['files'][1]}")
    return 0


def comply(cfg: Config, target: str, as_json: bool = False) -> int:
    """Whether the code of each requirement does all it asks, judged by the agent
    compliance_checker: the verdicts on the screen and the report in <workspace>/reports."""
    from atlas.agents.comply import comply_all
    _logging(cfg)
    res = comply_all(target, cfg)
    if as_json:
        return _json_result(res)
    if "error" in res:
        print(res["error"] + (f"; modules: {', '.join(res['modules'])}" if res.get("modules") else ""))
        return 1
    c = res["counts"]
    print(f"{len(res['results'])} requirements: {c['complies']} comply, {c['does_not_comply']} do not, "
          f"{c['cannot_tell']} cannot be decided by reading the code, {c['no_code']} without code "
          f"({res['asked']} asked to the model, the rest from the cache)" + _held(c))
    mark = {"met": "ok ", "not_met": "NO ", "partial": "1/2", "cannot_tell": " ? "}
    for r in res["results"]:
        if r.get("verdict") in ("does_not_comply", "cannot_tell"):
            print(f"\n{r['id']} ({', '.join(r['code'])}): {r['verdict']}. {r.get('summary', '')}")
            for o in r["obligations"]:
                if o["status"] != "met":
                    print(f"  {mark[o['status']]} {o['text']}: {o['evidence']}")
    print(f"\nwritten {res['files'][0]} and {res['files'][1]}")
    return 0


def link(cfg: Config, target: str, as_json: bool = False) -> int:
    """The code that may implement the requirements without code, judged by the agent
    req_linker: the proposals on the screen and the report in <workspace>/reports. Nothing
    is written in the code."""
    from atlas.agents.link import link_all
    from atlas.index import Index
    ctx = _context(cfg)
    index = Index.from_config(cfg) if ctx.store.code_units() else None
    res = link_all(target, cfg, index=index)
    if as_json:
        return _json_result(res)
    if "error" in res:
        print(res["error"] + (f"; modules: {', '.join(res['modules'])}" if res.get("modules") else ""))
        return 1
    c = res["counts"]
    print(f"{len(res['results'])} requirements: {c['proposed']} with a proposal, {c['none']} without a convincing "
          f"candidate, {c['no_candidates']} without candidates ({res['asked']} asked to the model, the rest from "
          f"the cache)" + _held(c))
    for r in res["results"]:
        for x in r.get("links", []):
            print(f"\n{r['id']} -> {x['code']} ({x['confidence']}): {x['reason']}\n  add `{x['tag']}` at {x['tag_at']}")
    print(f"\nnothing changed in the code; written {res['files'][0]} and {res['files'][1]}")
    return 0


def tests(cfg: Config, target: str) -> int:
    """The tests of the test procedures of a module (or of one), from their steps."""
    from atlas.code.procedures import generate_tests
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


def impact(cfg: Config, name: str, as_json: bool = False) -> int:
    from atlas.agents.reports import impact_report
    _context(cfg)
    res = impact_report(name, cfg)
    if as_json:
        return _json_result(res)
    if "error" in res:
        print(res["error"] + (f"; similar: {', '.join(res['similar'])}" if res.get("similar") else ""))
        return 1
    print(res["report"].strip() + f"\n\nwritten {res['file']}")
    return 0


def coverage(cfg: Config, module: str, as_json: bool = False) -> int:
    from atlas.agents.reports import coverage_report
    _context(cfg)
    res = coverage_report("" if module == "*" else module, cfg)
    if as_json:
        return _json_result(res)
    if "error" in res:
        print(res["error"] + (f"; modules: {', '.join(res['modules'])}" if res.get("modules") else ""))
        return 1
    print(res["report"].strip() + f"\n\nwritten {res['file']}")
    return 0


# options of the actions that take them: --name -> type (bool: a flag)
FILTERS = {"--module": str, "--chapter": str, "--attribute": str}
OPTIONS = {"init": {"--profile": str},
           "run": {"--until": str, "--max-minutes": int, "--max-requests": int},
           "estimate": {"--latency": float},
           "search": {"--k": int, "--kind": str, "--document": str, **FILTERS},
           "ask": {"--k": int, "--kind": str, "--document": str, "--whole": bool, "--section": str, "--dry-run": bool,
                   **FILTERS},
           "develop": {"--check": bool, "--resume": bool},
           "eval": {"--compare": bool, "--generate": str, "--n": int, "--set": str},
           "cache": {"--with-judgements": bool},
           "usage": {"--since": str},
           **{action: {"--json": bool} for action in ("status", "sections", "graph", "trace", "matrix", "verify",
                                                      "comply", "link", "coverage", "impact")}}
DESTINATIONS = {"--json": "as_json"}      # the argument of the function, when it is not the name of the option
REPEATABLE = {"--attribute": ";"}       # given twice: joined with this


def _options(action: str, args: list[str]) -> tuple[list[str], dict]:
    """The positional arguments and the --options of an action ({"k": 5, "whole": True,
    "as_json": True})."""
    allowed, positional, options = OPTIONS.get(action, {}), [], {}
    it = iter(args)
    for arg in it:
        if not arg.startswith("--") or arg == "--":
            positional.append(arg)
            continue
        name, has_value, value = arg.partition("=")
        if name not in allowed:
            sys.exit(f"atlas {action}: unknown option {name}"
                     + (f" (valid: {', '.join(allowed)})" if allowed else " (it takes none)"))
        kind, dest = allowed[name], DESTINATIONS.get(name, name[2:].replace("-", "_"))
        if kind is bool:
            options[dest] = True
            continue
        if not has_value:
            value = next(it, None)
            if value is None:
                sys.exit(f"atlas {action}: {name} needs a value")
        if name in REPEATABLE and name[2:] in options:
            value = f"{options[name[2:]]}{REPEATABLE[name]}{value}"
        try:
            options[dest] = kind(value)
        except ValueError:
            sys.exit(f"atlas {action}: {name} must be "
                     + {int: "an integer", float: "a number"}.get(kind, "a text"))
    return positional, options


def _kinds(kind: str) -> list[str] | None:
    from atlas.index import KINDS
    if kind and kind not in KINDS:
        sys.exit(f"unknown kind '{kind}' (valid: {', '.join(KINDS)})")
    return [kind] if kind else None


def _where(attribute: str, module: str, chapter: str) -> dict | None:
    from atlas.index import filters
    try:
        return filters(attribute, module, chapter) or None
    except ValueError as e:
        sys.exit(str(e))


def _no_match(index, where: dict) -> None:
    """What the filters can take, when they match nothing."""
    known = index.meta_values()
    unknown = [key for key in where if key not in known]
    if unknown:
        print(f"no passage has {', '.join(unknown)}")
    print("filters: " + ("; ".join(f"{key} ({', '.join(values[:10])})" for key, values in known.items())
                         or "none (has `atlas run` indexed anything since atlas 0.3?)"))


def search(cfg: Config, query: str, k: int = 0, kind: str = "", document: str = "", attribute: str = "",
           module: str = "", chapter: str = "") -> int:
    from atlas.index import Index, cite
    kinds, where = _kinds(kind), _where(attribute, module, chapter)
    _context(cfg)
    index = Index.from_config(cfg)
    hits = index.search(query, k=k, document=document, kinds=kinds, where=where)
    if not hits:
        print("nothing found" + (" with those filters" if where else " (has `atlas run` indexed any document?)"))
        if where:
            _no_match(index, where)
    for i, h in enumerate(hits, 1):
        text = h["text"][:400].replace("\n", "\n    ")
        print(f"\n[{i}] {cite(h)}\n    {text}")
    return 0


def _plan(res: dict) -> str:
    """The plan of a dry run of ask, for a person."""
    plan = res["plan"]
    if not plan:
        return (f"No document to read whole: the question would be answered from passages "
                f"({res['passages_requests']} requests to the model).")
    minutes = f", at least {plan['minutes']} min of the quota" if "minutes" in plan else ""
    section = f" › {plan['section']}" if plan.get("section") else ""
    lines = ["Plan, without asking the model (if the question is broad, or with --whole or --section):",
             f"  read whole: {', '.join(plan['documents'])}{section}",
             f"  sections: {plan['sections']}, characters: {plan['chars']}, parts: {plan['parts']}",
             f"  requests to the model: at least {plan['requests']}{minutes}"]
    if plan["parts"] > res["max_parts"]:
        lines.append(f"More than [search] max_parts = {res['max_parts']}: it would not be read; narrow it "
                     f"with --document and --section.")
    if not section:
        lines.append(f"Not broad: from passages, {res['passages_requests']} requests to the model.")
    return "\n".join(lines)


def ask(cfg: Config, question: str, k: int = 0, kind: str = "", document: str = "", whole: bool = False,
        section: str = "", dry_run: bool = False, attribute: str = "", module: str = "", chapter: str = "") -> int:
    from atlas.index import DocTools
    from atlas.index.search import citation_warnings, status_line
    from atlas.llm import usage, usage_since
    from atlas.usage import usage_sink
    kinds, where = _kinds(kind), _where(attribute, module, chapter)
    _context(cfg)
    before = usage()
    tools = DocTools.from_config(cfg)
    answer = tools.index.answer
    usage_log = usage_sink(cfg, "cli")
    if usage_log is not None and not dry_run:          # the row of atlas ask, with the status of its answer
        answer = plugins.instrumented(answer, usage_log, name="ask")
    # with filters, only what they let through: no requirement routed from outside them
    res = answer(question=question, k=k, document=document, kinds=kinds, scope="whole" if whole else "",
                 extra_sources=[] if where else tools.route(question), where=where, dry_run=dry_run, section=section)
    used = _api_text(usage_since(before))
    if dry_run:
        print(_plan(res))
        return 0
    if "error" in res:
        print(res["error"])
        if res.get("chapters"):
            print("\nSections (characters):")
        for c in res.get("chapters", []):
            print(f"  {c['document']} › {c['section']} ({c['chars']})")
        return 1
    print(res["answer"])
    if res.get("truncated"):
        print("\n(The answer is cut: the model reached max_tokens even when asked again with twice the "
              "budget. Raise [llm] max_tokens.)")
    if res["sources"]:
        print("\nSources:")
    for s in res["sources"]:
        mark = "local" if s.get("local_only") else s["n"]
        print(f"  [{mark}] {s['cite']}" + (f"\n       {s['file']}" if s["file"] else ""))
    if any(s.get("local_only") for s in res["sources"]):
        print("\n[local]: of a local-only source (model = \"none\"), found but not sent to the model")
    warnings = citation_warnings(res)
    if warnings:
        print("\n" + "\n".join(warnings))
    for u in res["unsupported"]:
        print(f"  - {u['claim'][:300]}\n    not in {' '.join(f'[{n}]' for n in u['cited'])}: {', '.join(u['missing'])}")
    if res["scope"] == "whole":
        print(f"\nRead whole: {', '.join(res['documents'])} ({res['requests']} requests to the model)")
    if used:
        print(f"\nModel: {used}")
    print(f"\n{status_line(res)}")
    return 1 if res["status"] == "not_found" else 0


def list_plugins(cfg: Config) -> int:
    ctx = _context(cfg)
    for kind, names in plugins.describe().items():
        print(f"{kind:16s} {', '.join(names) or '-'}")
        if kind == "extractor":
            for name in names:
                cls = plugins.get("extractor", name)
                off = " (disabled)" if f"extractor:{name}" in ctx.disabled else ""
                print(f"  {name:14s} {' '.join(cls.extensions)}  license: {cls.license or 'no third-party library'}{off}")
    if cfg["sources"].get("code"):
        from atlas.code.gpr import code_config
        sc = code_config(cfg)
        project = sc.get("_project")
        if project:
            scenario = ", ".join(f"{k}={v}" for k, v in project["externals"].items())
            print(f"\n{'code project':16s} {project['path']}" + (f" ({scenario})" if scenario else ""))
            for label, key in (("  imports", "withs"), ("  object dirs", "object_dirs")):
                if project[key]:
                    print(f"{label:16s} {', '.join(project[key])}")
        else:
            print()
        given = "" if project is None or (cfg["sources"]["code"].get("roots")) else " (from the project)"
        print(f"{'code roots':16s} {', '.join(sc.get('roots', [])) or '-'}{given}")
        ada = code_files(cfg, sc, (".ads", ".adb"))
        print(f"{'code files':16s} {len(ada)} .ads/.adb ({sum(f.lower().endswith('.ads') for f in ada)} .ads)")
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


def agent(cfg: Config, name: str, question: str) -> int:
    from atlas.agents.runtime import run_agent
    try:
        res = run_agent(name, question, cfg, _context(cfg))
    except ImportError as e:            # without the [agents] extra
        sys.exit(str(e))
    print(res["answer"] or "(no answer)")
    if res["stopped"]:
        print("\n(stopped after the most calls to the model of the agent, max_steps)")
    if res["calls"]:
        print("\nTools called:")
    for c in res["calls"]:
        print(f"  {c['name']}({json.dumps(c['args'], ensure_ascii=False)})")
    tokens = res.get("tokens") or {}
    print(f"\nModel: {tokens.get('prompt', 0)} prompt / {tokens.get('completion', 0)} completion tokens"
          + (f", ${res['cost']:.4f}" if "cost" in res else ""))
    return 1 if res["stopped"] else 0


def _develop_check(cfg: Config, plan: str) -> int:
    """What a plan starts from, without a model: its agents and tests checked, check_code on
    its files and its tests run once (Developer.check)."""
    from atlas.agents.develop import Developer, load_plan
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
    """The tasks of a plan, then the rounds that fix the failing tests (atlas/agents/develop.py);
    with `check`, only what the plan starts from; with `resume`, from where it stopped."""
    from atlas.agents.develop import develop as run_plan

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
              + (f"\n      budget spent: {s['budget_exceeded']}" if s.get("budget_exceeded") else ""), flush=True)

    _context(cfg)
    try:
        if check:
            return _develop_check(cfg, plan)
        report = run_plan(plan, cfg, on_step=show, resume=resume)
    except ImportError as e:            # without the [agents] extra
        sys.exit(str(e))
    except KeyboardInterrupt:
        print(f"\ninterrupted: the files of the step are as they were before it; `atlas develop {plan} "
              f"{cfg.path.name} --resume` goes on from there")
        return 130
    final, totals = report["final"], report["totals"]
    print(f"\n{final['tests_passed']} of {final['tests']} tests pass; {len(report['steps'])} agent runs, "
          f"{totals['seconds'] // 60} min of runs, {totals.get('wall_seconds', 0) // 60} min in all"
          + (f", ${totals['cost']}" if "cost" in totals else "")
          + (f"; {final['errors']} runs failed with an error" if final.get("errors") else ""))
    for test, why in (report.get("for_a_person") or {}).items():
        print(f"for a person: {test} ({why['reason']})")
    created = [f for s in report["steps"] if s.get("outcome") not in ("undone", "not chosen")
               for f in s.get("created", [])]
    if created:
        print(f"new files: {', '.join(created)}")
    print(f"report: {cfg.workspace / 'reports' / ('develop-' + report['plan'] + '.md')}")
    return 0 if final["tests_passed"] == final["tests"] else 1


def _hits(m: dict) -> str:
    return ", ".join(f"hit@{k} {m[f'hit@{k}']:.0%}" for k in (1, 3, 5, 10)) + f", MRR {m['mrr']:.2f}"


def _print_comparison(cmp: dict) -> None:
    """Each case against the same case of the report of another version, and the regressions."""
    from atlas.agents.evaluate import SEARCH_MODES
    against = cmp["against"]
    print(f"  compared with atlas {against.get('version', '?')} of {against.get('date', '?')}:")

    def got(c: dict | None) -> str:
        if c is None:
            return "-"
        if c["kind"] == "search":
            return " ".join(f"{m} {c[m] or '-'}" for m in SEARCH_MODES)
        return f"{c['passed']}/{c['runs']}" if "runs" in c else ("ok" if c["passed"] else "fail")

    for c in cmp["cases"]:
        print(f"    {c['change'].upper() if c['change'] == 'regression' else c['change']:10s} {c['id']}  "
              f"{got(c['before'])} -> {got(c['after'])}")
    if cmp["removed"]:
        print(f"    no longer in the set: {', '.join(cmp['removed'])}")
    print(f"  regressions: {', '.join(cmp['regressions']) if cmp['regressions'] else 'none'}")


def generate_set(cfg: Config, path: str, n: int) -> int:
    """atlas eval --generate: a set of search and graph cases from the workspace."""
    from atlas.agents.evaluate import GENERATE_KINDS, generate_cases, write_cases
    cases = generate_cases(cfg, n)
    if not cases:
        print("nothing to generate cases from: no requirement, section or subprogram with callers in "
              f"{cfg.workspace / 'atlas.db'} (has `atlas run` processed the sources?)", file=sys.stderr)
        return 1
    written = write_cases(path, cases, cfg)
    kinds = {k: sum(1 for c in cases if c["tags"][1] == k) for k in GENERATE_KINDS}
    print(f"written {written}: {len(cases)} cases (" + ", ".join(f"{v} {k}" for k, v in kinds.items()) + ")")
    print(f"add it to [agents.eval] sets of {cfg.path.name} and run `atlas eval {cfg.path.name}`")
    return 0


def hints(cfg: Config, plan: str) -> int:
    """Drafts of hints from the calls of the last atlas develop of a plan (atlas/agents/hints.py)."""
    from atlas.agents.hints import write_hints_draft
    calls = cfg.workspace / "reports" / f"develop-{Path(plan).stem}.calls.jsonl"
    if not calls.is_file():
        sys.exit(f"no calls of atlas develop for {Path(plan).stem}: {calls} does not exist")
    path, drafts = write_hints_draft(calls)
    for d in drafts:
        fixed = f", fixed in run {d['fix']['run']}" if d["fix"] else ""
        print(f"{d['count']:4d}  {d['message'][:100]}{fixed}")
    print(f"\n{len(drafts)} errors without a hint; drafts in {path}: write the hints there and copy them to a "
          "file of [sources.code] hints" if drafts else "every error of the compiler came with a hint")
    return 0


def evaluate(cfg: Config, compare: bool = False, generate: str = "", n: int = 0, set: str = "") -> int:
    """The sets of [agents.eval] sets, or only `set` (--set FILE, relative to atlas.toml)."""
    from atlas import __version__
    from atlas.agents.evaluate import SEARCH_MODES, evaluate_sets, previous_report
    if n and not generate:
        sys.exit("atlas eval: --n goes with --generate FILE")
    if generate:
        if n < 0 or compare:
            sys.exit("atlas eval: --generate takes --n (how many cases, 50 by default), not --compare")
        return generate_set(cfg, generate, n or 50)

    def show(r: dict) -> None:
        if r.get("kind") == "develop":
            e, c = r.get("edits") or {}, r.get("compiles") or {}
            print(f"{'ok  ' if r['passed'] else 'FAIL'} {r['id']} #{r['run']} {r['seconds']:.0f}s "
                  + (f"{r['tests_passed']} of {r['tests']} tests (expected {r['expect_tests_passed']}), "
                     f"{r['calls']} calls, edits {e.get('made', 0) + e.get('failed', 0)} ({e.get('failed', 0)} failed), "
                     f"compilations with errors {c.get('failed', 0)}" + (f", ${r['cost']}" if "cost" in r else "")
                     if "error" not in r else r["error"]), flush=True)
        elif r.get("kind") == "search":
            ranks = "  ".join(f"{m} {r[m]['rank'] or '-'}" for m in SEARCH_MODES)
            lost = sorted({x for m in SEARCH_MODES for x, n in r[m]["ranks"].items() if not n})
            print(f"{'ok  ' if r['hybrid']['rank'] else 'MISS'} {r['id']}  position: {ranks}"
                  + (f"  not found: {', '.join(lost)}" if lost else "")
                  + (f"  {r['hybrid_error']}" if r.get("hybrid_error") else ""), flush=True)
        elif r.get("kind") == "graph":
            print(f"{'ok  ' if r['passed'] else 'FAIL'} {r['id']}  {r['query']}"
                  + (f"  missing: {', '.join(r['missing'])}" if r["missing"] else "")
                  + (f"  {r['error']}" if r.get("error") else ""), flush=True)
        elif r.get("kind") == "ask":
            print(f"{'ok  ' if r['passed'] else 'FAIL'} {r['id']} #{r['run']} {r['seconds']:.0f}s "
                  f"[{r['scope'] or '-'}, {r['requests']} requests]"
                  + (f"  missing: {', '.join(r['missing'])}" if r["missing"] else "")
                  + (f"  not in a cited source: {', '.join(r['uncited'])}" if r["uncited"] else "")
                  + (f"  {r['error']}" if r.get("error") else ""), flush=True)
        else:
            missing = r["missing"] + [f"tool {t}" for t in r["missing_tools"]] + (["max_steps"] if r["stopped"] else [])
            print(f"{'ok  ' if r['passed'] else 'FAIL'} {r['id']} #{r['run']} {r['seconds']:.0f}s "
                  f"[{', '.join(r['calls'])}]" + (f"  missing: {', '.join(missing)}" if missing else "")
                  + (f"  {r['error']}" if r.get("error") else ""), flush=True)

    try:
        results = evaluate_sets(cfg, context=_context(cfg), on_result=show, sets=[set] if set else None)
    except ImportError as e:
        sys.exit(str(e))
    regressions = 0
    for name, res in results.items():
        print(f"\n{name} (written to {cfg.workspace / 'reports' / f'eval-{name}.json'}):")
        found = res.get("retrieval", {})
        if "search" in found:
            for mode in SEARCH_MODES:
                print(f"  search, {mode}: {_hits(found['search'][mode])} ({found['search']['cases']} cases)")
            for tag, t in found["search"].get("tags", {}).items():
                print(f"    {tag} ({t['cases']}): " + "  ".join(
                    f"{mode} hit@3 {t[mode]['hit@3']:.0%} MRR {t[mode]['mrr']:.2f}" for mode in SEARCH_MODES))
        if "graph" in found:
            g = found["graph"]
            print(f"  graph: {g['passed']} of {g['total']} right ({g['rate']:.0%})")
            for tag, t in g.get("tags", {}).items():
                print(f"    {tag}: {t['passed']} of {t['total']}")
        if "answers" in res:
            a = res["answers"]
            print(f"  answers: {a['passed']} of {a['total']} right ({a['rate']:.0%}), median "
                  f"{a['median_seconds']:.0f} s, {a['requests']} requests to the model")
        if "agents" in res:
            a = res["agents"]
            tokens = a.get("tokens") or {}
            print(f"  agents: {a['passed']} of {a['total']} right ({a['rate']:.0%}), median {a['median_seconds']:.0f} s"
                  + (f", {tokens['prompt']} + {tokens['completion']} tokens" if tokens.get("prompt") else "")
                  + (f", ${a['cost']:.4f}" if "cost" in a else ""))
        if "develop" in res:
            d, totals = res["develop"], res["develop"]["totals"]
            print(f"  develop: {d['passed']} of {d['total']} runs reached their tests ({d['rate']:.0%}); "
                  f"{totals['tests_passed']} of {totals['tests']} tests, {totals['calls']} calls, "
                  f"{totals['edits']['failed']} of {totals['edits']['made'] + totals['edits']['failed']} edits failed, "
                  f"{totals['seconds'] / 60:.1f} min" + (f", ${totals['cost']}" if "cost" in totals else ""))
        for section in ("answers", "agents"):
            for tag, t in res.get(section, {}).get("tags", {}).items():
                print(f"    {section} {tag}: {t['passed']} of {t['total']}")
        if _api_text(res.get("usage", {})):
            print(f"  model: {_api_text(res['usage'])}")
        if compare:
            old = previous_report(cfg, name, __version__)
            if old is None:
                print(f"  nothing to compare with: no report of {name} from a version other than {__version__}")
            else:
                from atlas.agents.evaluate import compare_reports
                cmp = compare_reports(old, res)
                _print_comparison(cmp)
                regressions += len(cmp["regressions"])
    return 1 if regressions else 0


def usage(cfg: Config, since: str = "30d") -> int:
    """What the usage log of the workspace says of the last days (atlas.usage.summary)."""
    from atlas.usage import parse_since, summary
    _logging(cfg)
    try:
        days = parse_since(since)
    except ValueError as e:
        sys.exit(f"atlas usage: {e}")
    res = summary(cfg.workspace / "atlas.db", days)
    print(f"usage log of {cfg.workspace} since {res['since']}: {res['calls']} calls "
          f"([mcp] usage_log = {cfg['mcp']['usage_log']})")
    if not res["calls"]:
        print("nothing logged in those days")
        return 0
    weeks = res["weeks"][-8:]
    print(f"\n  {'origin':6s} {'tool or command':24s} " + " ".join(f"{w[-3:]:>5s}" for w in weeks)
          + f" {'calls':>6s} {'errors':>6s} {'median ms':>9s}")
    for r in res["by_name"]:
        print(f"  {r['origin']:6s} {r['name'][:24]:24s} " + " ".join(f"{r['weeks'].get(w, 0):5d}" for w in weeks)
              + f" {r['calls']:6d} {r['errors']:6d} {r['median_ms']:9d}")
    used = [r for r in res["by_name"] if r["requests"]]
    if used:
        print("\nRequests to the model, tokens and cost:")
        for r in used:
            print(f"  {r['origin']:6s} {r['name'][:24]:24s} {r['requests']} requests"
                  + (f", {_tokens_text(r)}" if _tokens_text(r) else ""))
        total = res["total"]
        if total["requests"]:
            print(f"  {'total (cli and mcp)':31s} {total['requests']} requests"
                  + (f", {_tokens_text(total)}" if _tokens_text(total) else ""))
    if res["answers"]:
        print("\nAnswers:")
        for name, counts in res["answers"].items():
            print(f"  {name:31s} " + ", ".join(f"{n} {status.replace('_', ' ')}" for status, n in sorted(counts.items())))
    return 0


def check(cfg: Config) -> int:
    from atlas.config import unset_hint
    from atlas.llm import check as check_connection, from_config
    _logging(cfg)
    if cfg.unset:
        print(f"WARNING {unset_hint(cfg)}", file=sys.stderr)
    from atlas.index.embeddings import prefix_note
    conn = from_config(cfg)
    return check_connection(conn, {"Embeddings": [prefix_note(conn.embeddings)]})


def doctor(cfg: Config) -> int:
    """A table of what can fail with this configuration on this machine; 1 if something fails."""
    from atlas.doctor import diagnose, print_findings
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    findings = diagnose(cfg)
    print_findings(findings)
    return 1 if any(f.level == "fail" for f in findings) else 0


def probe(cfg: Config) -> int:
    """The chat model as an agent: a tool call, its result, the reasoning given back and streaming."""
    from atlas.llm import from_config
    from atlas.llm.probe import probe as probe_model
    _logging(cfg)
    return probe_model(from_config(cfg).llm)


def reference(cfg: Config) -> int:
    """The manuals and the library for search_reference: built, or brought up to date."""
    from atlas.code import BuildTools
    _logging(cfg)
    tools = BuildTools.from_config(cfg)
    if not (tools.reference.patterns or tools.library.folders):
        print("nothing to index: [sources.code] reference (manuals) and library (specs)", file=sys.stderr)
        return 1
    for name, records in tools.reference.ensure().items():
        print(f"{name:30s} {records} passages" if name != "library" else f"{name:30s} {records} declarations")
    print(f"index: {tools.reference.db}")
    return 0


def serve(cfg: Config) -> int:
    """The MCP server over stdio, until the client closes it."""
    from atlas.mcp import serve as serve_mcp
    try:
        serve_mcp(cfg, _context(cfg))
    except ImportError as e:            # without the mcp package
        sys.exit(str(e))
    return 0


def cache(cfg: Config, command: str, file: str, with_judgements: bool = False) -> int:
    """atlas cache export|import FILE: the caches of the models, from one workspace to others
    (atlas/cache.py)."""
    from atlas.cache import CacheError, pack, unpack
    if command == "export":
        if not (cfg.workspace / "atlas.db").exists():         # before _context, which would create it
            raise CacheError(f"nothing to export: {cfg.workspace} has no atlas.db (run `atlas run` first)")
        _context(cfg)
        head = pack(cfg, file, with_judgements)
        c = head["counts"]
        print(f"exported to {file}: {c['vectors']} vectors ({head['embeddings_model']}), {c['analyses']} analyses "
              f"of sections ({head['chat_model']})" + (f", {c['judgements']} judgements" if with_judgements else ""))
        if head["unattributed"]:
            print(f"{head['unattributed']} cached vectors of an unknown model (texts no longer in the index) "
                  f"were not exported", file=sys.stderr)
        return 0
    if with_judgements:
        sys.exit("atlas cache import: --with-judgements is an option of export (import takes what the file has)")
    _context(cfg)
    res = unpack(cfg, file)
    head = res["header"]
    print(f"imported from {file} (project {head['project']}, atlas {head['atlas_version']}, {head['created']}):")
    print(f"  vectors ({head['embeddings_model']}): {res['vectors']['new']} new, "
          f"{res['vectors']['present']} already here")
    print(f"  analyses of sections ({head['chat_model']}): {res['analyses']['new']} new, "
          f"{res['analyses']['present']} already here")
    for name, n in res["judgements"].items():
        print(f"  judgements {name}: {n['new']} new, {n['present']} already here")
    for w in res["warnings"]:
        print(f"WARNING {w}", file=sys.stderr)
    return 0


TEMPLATES = {"ollama": Path(__file__).parent / "templates" / "atlas.toml",       # a local model (the default)
             "gateway": Path(__file__).parent / "templates" / "atlas-gateway.toml"}


def init(path: Path, profile: str = "ollama") -> int:
    """Writes the template of atlas.toml for `profile` in `path`, unless the file exists."""
    if profile not in TEMPLATES:
        print(f"unknown profile '{profile}' (valid: {', '.join(TEMPLATES)})", file=sys.stderr)
        return 1
    if path.exists():
        print(f"{path} already exists: it is not overwritten", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATES[profile].read_text(encoding="utf-8"), encoding="utf-8")
    print(f"created {path}: edit the sources and the model, then `atlas doctor {path.name}` and "
          f"`atlas run {path.name}`")
    return 0


UNLOGGED = ("serve", "usage", "ask")     # serve: each tool call; ask: its answer (see ask)


def _logged(action: str, function, cfg: Config):
    """The action, with a row of the usage log of the workspace (origin "cli"): its time,
    whether it raised, and the requests, tokens and cost of the whole command."""
    from atlas.usage import usage_sink
    usage_log = None if action in UNLOGGED else usage_sink(cfg, "cli")
    return plugins.instrumented(function, usage_log, name=action) if usage_log is not None else function


def main(argv: list[str] | None = None) -> None:
    # A Windows console, or its output redirected to a file, is cp1252: a requirement with
    # "→" or "≤" would end the action with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if argv[0] in ("-V", "--version", "version"):
        from atlas import __version__
        print(f"atlas {__version__}")
        sys.exit(0)
    action, rest = argv[0], argv[1:]
    from atlas.llm import set_usage_label
    set_usage_label(action)
    t0 = time.monotonic()
    try:
        _dispatch(action, rest)
    finally:                                  # the last line of the action in its log
        error = sys.exc_info()[1]
        how = f"exit code {error.code}" if isinstance(error, SystemExit) else \
            f"{type(error).__name__}: {error}" if error else "exit code 0"
        log.debug("end of %s: %s, %.1f s", action, how, time.monotonic() - t0)


def _dispatch(action: str, rest: list[str]) -> None:
    if action == "init":
        rest, options = _options(action, rest)
        sys.exit(init(Path(rest[0] if rest else "atlas.toml"), **options))
    actions = {"run": run, "estimate": estimate, "status": status, "sections": sections, "graph": graph,
               "plugins": list_plugins, "agents": list_agents, "eval": evaluate, "check": check, "doctor": doctor,
               "probe": probe, "reference": reference, "serve": serve, "usage": usage}
    with_text = {"search": search, "ask": ask, "trace": trace, "matrix": matrix, "verify": verify, "link": link, "comply": comply,
                 "tests": tests, "develop": develop, "hints": hints, "impact": impact, "coverage": coverage}
    if action == "agent":                       # two arguments: NAME TEXT [atlas.toml]
        if len(rest) < 2:
            sys.exit("usage: atlas agent NAME TEXT [atlas.toml]")
        try:
            cfg = load_config(rest[2] if len(rest) > 2 else "atlas.toml")
            sys.exit(_logged("agent", agent, cfg)(cfg, rest[0], rest[1]))
        except ConfigError as e:
            sys.exit(f"configuration: {e}")
    if action == "cache":                       # cache export|import FILE [atlas.toml]
        from atlas.cache import CacheError
        from atlas.pipeline import WorkspaceLocked
        rest, options = _options(action, rest)
        if len(rest) < 2 or rest[0] not in ("export", "import"):
            sys.exit("usage: atlas cache export|import FILE [atlas.toml] [--with-judgements]")
        try:
            sys.exit(cache(load_config(rest[2] if len(rest) > 2 else "atlas.toml"), rest[0], rest[1], **options))
        except ConfigError as e:
            sys.exit(f"configuration: {e}")
        except (CacheError, WorkspaceLocked) as e:
            sys.exit(f"atlas cache {rest[0]}: {e}")
    if action not in actions and action not in with_text:
        sys.exit(f"unknown action '{action}'. {__doc__}")
    rest, options = _options(action, rest)
    if action in with_text and not rest:
        sys.exit(f"usage: atlas {action} TEXT [atlas.toml]")
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
