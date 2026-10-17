"""A whole document, from the file to the Markdown output (`procesar` of legacy/pdf_struct.py).

    from atlas.docs import DocsOptions, process_document
    data = process_document("spec.pdf", "out/spec", DocsOptions(analysis="regex"))
    data = process_document("design.pdf", "out/design", DocsOptions(lang="Spanish"), llm=conn.llm)

The output folder is written in `<out>.tmp` and replaces `<out>` at the end, so a cut
never leaves it half written and no file of a previous run is left behind. The analysis
cache of the sections lives outside it (by default in `<out>/../.cache`): processing
the document again only asks the model for the sections whose text changed and for the
ones that failed.
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Iterable

from atlas.docs import analysis as an
from atlas.docs import extractor_for, read_text
from atlas.docs.structure import STRUCTURES, clean_headers, diagnose_headings, document_title, prepare_sections
from atlas.docs.writer import pages, write_document
from atlas.llm.connection import ServiceConfig, diagnose, is_connection_error
from atlas.llm.structured import JsonLlm
from atlas.pipeline import FatalError, Interrupted, atomic_write

__all__ = ["DocsOptions", "pending_requests", "preview", "process_document"]

log = logging.getLogger("atlas.docs")


@dataclass
class DocsOptions:
    """How to process the documents of a source: the processing keys of [[sources.docs]]."""
    analysis: str = "llm"           # llm | regex | none
    req_regex: str = an.DEFAULT_REQ_REGEX
    structure: str = "auto"         # auto | bookmarks | headings | size
    heading_regex: str = ""         # group 1 = number, group 2 = title
    lang: str = ""                  # language of summaries; "" = the one of the document
    max_chars: int = 12000          # largest section sent to the model
    min_chars: int = 300            # shorter sections are merged with the previous one
    id_regex: str = ""              # the IDs of a requirement (with regex): [sources.doors.convention]

    def __post_init__(self):
        if self.analysis not in an.ANALYSES:
            raise ValueError(f"analysis must be one of {', '.join(an.ANALYSES)}, not '{self.analysis}'")
        if self.structure not in STRUCTURES:
            raise ValueError(f"structure must be one of {', '.join(STRUCTURES)}, not '{self.structure}'")
        if self.max_chars < 500:
            raise ValueError(f"max_chars must be at least 500, not {self.max_chars}")
        for key in ("req_regex", "heading_regex", "id_regex"):
            try:
                re.compile(getattr(self, key))
            except re.error as e:
                raise ValueError(f"{key} is not a valid regular expression: {e}") from None

    @classmethod
    def from_source(cls, source: dict) -> "DocsOptions":
        """From a [[sources.docs]] table (the keys that are not processing options, such
        as name or paths, are ignored)."""
        keys = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in source.items() if k in keys})


def preview(path: str | Path, options: DocsOptions | None = None, disabled: Iterable[str] = ()) -> dict:
    """The sections a document would have, without analysing it (the --dry-run of the
    legacy script): title, extractor, structure source, sections and, when the structure
    did not come from the bookmarks or some section had to be split in many parts, the
    heading diagnosis."""
    path, options = Path(path), options or DocsOptions()
    text, removed = clean_headers(read_text(path, disabled))
    info: dict = {}
    sections = prepare_sections(text, options.max_chars, options.min_chars, options.heading_regex or None,
                                path, options.structure, info)
    out = {"title": document_title(text, path), "chars": len(text), "removed_lines": removed,
           "extractor": extractor_for(path, disabled).name, "structure": info["source"],
           "subdivided": info["subdivided"], "oversized": info["oversized"],
           "sections": [{"id": s["id"], "pages": pages(s), "chars": len(s["body"]), "path": s["path"],
                         "includes": s["includes"]} for s in sections]}
    if info["oversized"] or not info["source"].startswith("PDF bookmarks"):
        out["headings"] = diagnose_headings(text)
    return out


def pending_requests(path: str | Path, options: DocsOptions, model: str, cache_dir: str | Path,
                     disabled: Iterable[str] = ()) -> dict:
    """What processing a document now would ask the model, without asking it: its sections
    (as preview finds them), those whose analysis is not in the cache of `cache_dir` (the
    `cache_dir` of process_document) and whether the overview is missing too. It is 1
    request per section, unless an answer comes cut or broken and the section is split
    (one more per split), plus 1 for the overview (more for a document whose summaries do
    not fit in one request)."""
    path = Path(path)
    text, _ = clean_headers(read_text(path, disabled))
    doc_title = document_title(text, path)
    sections = prepare_sections(text, options.max_chars, options.min_chars, options.heading_regex or None,
                                path, options.structure, {})
    if options.analysis != "llm":
        return {"sections": len(sections), "to_analyse": 0, "overview": False}
    files = [an.section_cache_file(Path(cache_dir), model, s, doc_title, options.lang) for s in sections]
    analyses = [an._read_cache(f) for f in files]
    missing = sum(a is None for a in analyses)
    overview = bool(missing)              # its key depends on the summaries: not known before them
    if not missing:
        for s, a in zip(sections, analyses):
            s["analysis"] = a              # as _analyze_all takes it from the cache
        f = an.overview_cache_file(Path(cache_dir), model, sections, doc_title, options.lang)
        overview = an._read_cache(f) is None
    return {"sections": len(sections), "to_analyse": missing, "overview": overview}


def _replace_dir(tmp: Path, out: Path) -> None:
    old = out.with_name(out.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if out.exists():
        out.rename(old)
    tmp.rename(out)
    shutil.rmtree(old, ignore_errors=True)


def process_document(path: str | Path, out: str | Path, options: DocsOptions | None = None, *,
                     llm: ServiceConfig | JsonLlm | None = None, title: str | None = None,
                     cache_dir: str | Path | None = None, workers: int = 1,
                     disabled: Iterable[str] = (), stop: threading.Event | None = None,
                     shared_cache=None) -> dict:
    """Extracts, structures and analyses a document and writes its Markdown into `out`.
    Returns the content of structure.json plus `out_dir`, `errors` (sections, or the
    overview, whose analysis failed: processing again retries only those) and
    `structure_info`. A connection error with the model raises FatalError. Sections are
    analysed `workers` at a time; when `stop` is set (the run was interrupted), no other
    request starts and Interrupted is raised once the ones in flight end. `shared_cache`
    (atlas.cache.SharedCache) is looked up for the analyses the local cache lacks."""
    path, out = Path(path), Path(out)
    options = options or DocsOptions()
    if options.analysis == "llm":
        if llm is None:
            raise ValueError("analysis = 'llm' needs the model: pass llm=ServiceConfig or JsonLlm")
        if isinstance(llm, ServiceConfig):
            llm = JsonLlm(llm)
    t0 = time.time()

    text, removed = clean_headers(read_text(path, disabled))
    doc_title = title or document_title(text, path)
    info: dict = {}
    sections = prepare_sections(text, options.max_chars, options.min_chars, options.heading_regex or None,
                                path, options.structure, info)
    log.info("'%s': %d chars, %d sections; structure: %s%s%s", doc_title, len(text), len(sections),
             info["source"],
             f"; {info['subdivided']} large sections subdivided by their headings" if info["subdivided"] else "",
             f"; {removed} header/footer lines removed" if removed else "")
    for big, n in info["oversized"][:5]:
        log.warning("'%s' is split in %d parts: no headings recognised inside it", big[:60], n)

    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        atomic_write(tmp / "_extracted_text.md", text)
        errors, doc_overview, overview_error = 0, an.empty_overview(), ""
        if options.analysis == "none":
            for s in sections:
                s["analysis"] = an.normalize({})
        elif options.analysis == "regex":
            pattern = re.compile(options.req_regex)
            for s in sections:
                s["analysis"] = an.normalize({"requirements": an.regex_requirements(s["body"], pattern,
                                                                                    options.id_regex or None)})
        else:
            cache = Path(cache_dir) if cache_dir else out.parent / ".cache"
            errors = _analyze_all(llm, sections, doc_title, options.lang, cache, tmp / "_llm_failures", workers,
                                  stop, shared_cache)
            _check(stop, doc_title)
            try:
                doc_overview = an.overview(llm, sections, doc_title, options.lang, options.max_chars, cache,
                                           shared_cache)
            except Exception as e:
                if is_connection_error(e):
                    raise FatalError(f"connection error with the LLM: {diagnose(e)}") from e
                errors += 1
                overview_error = f"{type(e).__name__}: {e}"[:500]
                log.warning("overview of '%s' failed: %s", doc_title, overview_error)

        cons = an.consolidate(sections)
        data = write_document(tmp, doc_title, path.name, llm.model if options.analysis == "llm" else "",
                              sections, doc_overview, cons,
                              extra={"analysis_mode": options.analysis, "overview_error": overview_error})
        _replace_dir(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    status = {k: sum(1 for r in cons["requirements"] if r["status"] == k) for k in ("literal", "partial", "not_found")}
    log.info("'%s' done in %.0fs -> %s: %d sections (%d with errors), %d requirements %s, "
             "%d terms, %d entities, %d open issues", doc_title, time.time() - t0, out / "index.md",
             len(sections), errors, len(cons["requirements"]), status, len(cons["glossary"]),
             len(cons["entities"]), len(cons["open_issues"]))
    return {**data, "out_dir": str(out), "errors": errors, "structure_info": info}


def _check(stop: threading.Event | None, doc_title: str) -> None:
    if stop is not None and stop.is_set():
        raise Interrupted(f"'{doc_title}': the run was interrupted")


def _analyze_all(llm: JsonLlm, sections: list[dict], doc_title: str, lang: str, cache: Path,
                 failures: Path, workers: int, stop: threading.Event | None = None, shared=None) -> int:
    """Analyses every section, in parallel, from the cache when possible. A section that
    fails keeps an empty analysis and its error; a connection error stops everything, and
    so does `stop`."""
    cached = {s["id"] for s in sections
              if an.is_cached(an.section_cache_file(cache, llm.model, s, doc_title, lang), shared)}
    log.info("analysis: %d of %d sections already in the cache; %d to process",
             len(cached), len(sections), len(sections) - len(cached))
    t0, errors = time.time(), 0

    def one(s: dict) -> tuple[dict, float]:
        _check(stop, doc_title)
        t = time.monotonic()
        return an.analyze_section(llm, s, doc_title, lang, cache, failures, shared), time.monotonic() - t

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, s): s for s in sections}
        try:
            for n, fut in enumerate(as_completed(futures), 1):
                s = futures[fut]
                try:
                    s["analysis"], seconds = fut.result()
                    result = f"{len(s['analysis']['requirements'])} req." + (" (cache)" if s["id"] in cached else "")
                except Exception as e:
                    if is_connection_error(e):
                        raise FatalError(f"connection error with the LLM: {diagnose(e)}") from e
                    errors += 1
                    s["analysis"] = an.normalize({})
                    s["analysis_error"] = f"{type(e).__name__}: {e}"
                    result, seconds = f"ERROR: {e}", 0.0
                log.info("  [%d/%d] %s %s — %s (%.0fs, total %.0fs)", n, len(sections), s["id"], s["title"][:50],
                         result, seconds, time.time() - t0)
        except BaseException:
            # fatal error or Ctrl+C: the sections not started yet are cancelled (otherwise
            # leaving the `with` would wait for all of them); only the ones in progress finish
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    return errors
