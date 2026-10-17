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

import contextlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Iterable

from atlas.docs import analysis as an
from atlas.docs import extractor_for, read_document
from atlas.docs.structure import (STRUCTURES, clean_headers, diagnose_headings, document_metadata, document_title,
                                  prepare_sections)
from atlas.docs.writer import pages, write_document
from atlas.llm.connection import ServiceConfig, diagnose, is_connection_error
from atlas.llm.structured import JsonLlm
from atlas.pipeline import FatalError, Interrupted, atomic_write, sha256_file, sha256_text

__all__ = ["DocsOptions", "ScannedDocument", "pending_requests", "preview", "process_document"]

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
    ocr_command: list[str] = ()     # ["ocrmypdf", "--skip-text", "{file}", "{out}"]: for PDFs with scanned pages
    doc_id_regex: str = ""          # code, version and date of the document (group 1), from its headers
    version_regex: str = ""         # and first two pages: r"Doc\.\s+(\S+)", r"\bIssue\s+(\w+)"
    date_regex: str = ""
    tables: bool = False            # PDF tables as Markdown rows (page.find_tables)
    table_requirements: dict = field(default_factory=dict)     # {"id": "ID", "text": "Requirement"}: one per row

    def __post_init__(self):
        if self.analysis not in an.ANALYSES:
            raise ValueError(f"analysis must be one of {', '.join(an.ANALYSES)}, not '{self.analysis}'")
        if self.structure not in STRUCTURES:
            raise ValueError(f"structure must be one of {', '.join(STRUCTURES)}, not '{self.structure}'")
        if self.max_chars < 500:
            raise ValueError(f"max_chars must be at least 500, not {self.max_chars}")
        if self.ocr_command and (isinstance(self.ocr_command, str)
                                 or not all(isinstance(x, str) for x in self.ocr_command)
                                 or not any("{file}" in x for x in self.ocr_command)
                                 or not any("{out}" in x for x in self.ocr_command)):
            raise ValueError('ocr_command must be a list of strings with "{file}" and "{out}", '
                             'such as ["ocrmypdf", "--skip-text", "{file}", "{out}"]')
        self.ocr_command = list(self.ocr_command)
        if self.table_requirements and (set(self.table_requirements) != {"id", "text"} or not all(
                isinstance(v, str) and v.strip() for v in self.table_requirements.values())):
            raise ValueError('table_requirements must name the columns of the ID and of the text, such as '
                             '{ id = "ID", text = "Requirement" }')
        for key in ("req_regex", "heading_regex", "id_regex", "doc_id_regex", "version_regex", "date_regex"):
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


class ScannedDocument(RuntimeError):
    """More than half of the pages of a PDF are images without text (a scan without OCR):
    nothing of it would reach the model or the index. The unit stays in error, and is
    retried once the PDF has text or the source has an `ocr_command`."""


def scanned(info: dict) -> str:
    """The warning if more than half of the pages have no text, else ""."""
    pages, without = info.get("pages", 0), info.get("pages_without_text", 0)
    if pages and without * 2 > pages:
        return f"scanned PDF: {without} of {pages} pages have no text; nothing is sent to the model"
    return ""


def ocr_copy(path: Path, command: list[str], folder: Path) -> Path:
    """The PDF after `command` ({file}: a copy of `path`, {out}: the PDF to write), kept in
    `folder` by the hash of the PDF and of the command: the original is never touched, and
    the OCR (minutes for a long scan) runs once per version of the file."""
    folder.mkdir(parents=True, exist_ok=True)
    key = sha256_text(sha256_file(path), json.dumps(command))[:32]
    out = folder / f"{key}.pdf"
    if out.exists():
        return out
    work = Path(tempfile.mkdtemp(dir=folder, prefix=f"{key}."))
    try:
        src, dst = work / f"in{path.suffix.lower()}", work / "out.pdf"
        shutil.copyfile(path, src)
        args = [a.replace("{file}", str(src)).replace("{out}", str(dst)) for a in command]
        t0 = time.time()
        try:
            r = subprocess.run(args, capture_output=True, text=True, errors="replace")
        except FileNotFoundError:
            raise RuntimeError(f"ocr_command: '{command[0]}' not found") from None
        if r.returncode != 0 or not dst.exists():
            detail = (r.stderr or r.stdout or "").strip()[-300:]
            raise RuntimeError(f"ocr_command failed (exit code {r.returncode})" + (f": {detail}" if detail else ""))
        dst.replace(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log.info("'%s': OCR in %.0fs -> %s", path.name, time.time() - t0, out)
    return out


def extract(path: Path, options: DocsOptions, disabled: Iterable[str], ocr_dir: Path) -> tuple[str, dict, Path]:
    """(text, what the extractor knows of it, the PDF to read the bookmarks from). A PDF
    with pages without text goes through the `ocr_command` of the options first, if there
    is one (into `ocr_dir`): then the text is that of the copy, and `ocr` in the info says
    which file it is. Without it, or if after it most pages still have no text, the info
    has `scanned` (the warning)."""
    config = {"tables": options.tables}
    text, info = read_document(path, disabled, config)
    pdf = path
    if info.get("pages_without_text") and options.ocr_command and path.suffix.lower() == ".pdf":
        pdf = ocr_copy(path, options.ocr_command, ocr_dir)
        text, info = read_document(pdf, disabled, config)
        info["ocr"] = str(pdf)
    if scanned(info):
        info["scanned"] = scanned(info)
    return text, info, pdf


def _metadata(raw: str, extracted: dict, options: DocsOptions) -> dict[str, str]:
    return document_metadata(raw, extracted.get("metadata"), options.doc_id_regex, options.version_regex,
                             options.date_regex)


def preview(path: str | Path, options: DocsOptions | None = None, disabled: Iterable[str] = (),
            ocr_dir: str | Path | None = None) -> dict:
    """The sections a document would have, without analysing it (the --dry-run of the
    legacy script): title, extractor, structure source, sections and, when the structure
    did not come from the bookmarks or some section had to be split in many parts, the
    heading diagnosis; `doc_id`, `version` and `date` as in structure.json.
    `pages_without_text` counts the pages that are only an image, and `scanned` warns
    when they are more than half. With `ocr_command`, the copies with OCR
    are kept in `ocr_dir` (by default, in a temporary folder that is removed)."""
    path, options = Path(path), options or DocsOptions()
    with tempfile.TemporaryDirectory() if ocr_dir is None else contextlib.nullcontext(ocr_dir) as folder:
        raw, extracted, pdf = extract(path, options, disabled, Path(folder))
        metadata = _metadata(raw, extracted, options)
        text, removed = clean_headers(raw)
        info: dict = {}
        sections = prepare_sections(text, options.max_chars, options.min_chars, options.heading_regex or None,
                                    pdf, options.structure, info)
    out = {"title": document_title(text, path), **metadata, "chars": len(text), "removed_lines": removed,
           "extractor": extractor_for(path, disabled).name,
           **{k: extracted[k] for k in ("pages", "pages_without_text", "scanned", "ocr", "tables") if k in extracted},
           "structure": info["source"],
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
    text, _ = clean_headers(read_document(path, disabled, {"tables": options.tables})[0])
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
                     shared_cache=None, ocr_dir: str | Path | None = None) -> dict:
    """Extracts, structures and analyses a document and writes its Markdown into `out`.
    Returns the content of structure.json plus `out_dir`, `errors` (sections, or the
    overview, whose analysis failed: processing again retries only those),
    `structure_info` and `sent`: the sections and overviews asked to the model (not from the
    cache). A connection error with the model raises FatalError. Sections are
    analysed `workers` at a time; when `stop` is set (the run was interrupted), no other
    request starts and Interrupted is raised once the ones in flight end. `shared_cache`
    (atlas.cache.SharedCache) is looked up for the analyses the local cache lacks.

    A PDF whose pages are mostly images without text raises ScannedDocument before
    anything is written or sent; with `ocr_command` in the options, it is read from a
    copy with OCR, kept in `ocr_dir` (by default `<out>/../.ocr`)."""
    path, out = Path(path), Path(out)
    options = options or DocsOptions()
    if options.analysis == "llm":
        if llm is None:
            raise ValueError("analysis = 'llm' needs the model: pass llm=ServiceConfig or JsonLlm")
        if isinstance(llm, ServiceConfig):
            llm = JsonLlm(llm)
    t0 = time.time()

    raw, extracted, pdf = extract(path, options, disabled, Path(ocr_dir) if ocr_dir else out.parent / ".ocr")
    if extracted.get("scanned"):
        raise ScannedDocument(extracted["scanned"])
    if extracted.get("pages_without_text"):
        log.warning("'%s': %d of %d pages have no text (scanned?)", path.name, extracted["pages_without_text"],
                    extracted["pages"])
    metadata = _metadata(raw, extracted, options)
    text, removed = clean_headers(raw)
    doc_title = title or document_title(text, path)
    info: dict = {k: extracted[k] for k in ("pages", "pages_without_text", "ocr", "tables") if extracted.get(k)}
    sections = prepare_sections(text, options.max_chars, options.min_chars, options.heading_regex or None,
                                pdf, options.structure, info)
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
        sent: dict[str, list] = {"sections": [], "overviews": []}
        if options.analysis == "none":
            for s in sections:
                s["analysis"] = an.normalize({})
        elif options.analysis == "regex":
            pattern = re.compile(options.req_regex)
            for s in sections:
                s["analysis"] = an.normalize({"requirements": an.regex_requirements(s["body"], pattern,
                                                                                    options.id_regex or None)})
                if options.table_requirements:
                    s["analysis"] = an.add_requirements(s["analysis"],
                                                        an.table_requirements(s["body"], options.table_requirements))
        else:
            cache = Path(cache_dir) if cache_dir else out.parent / ".cache"
            errors = _analyze_all(llm, sections, doc_title, options.lang, cache, tmp / "_llm_failures", workers,
                                  stop, shared_cache, sent["sections"])
            if options.table_requirements:      # the rows the model skipped
                for s in sections:
                    s["analysis"] = an.add_requirements(s["analysis"],
                                                        an.table_requirements(s["body"], options.table_requirements))
            _check(stop, doc_title)
            try:
                doc_overview = an.overview(llm, sections, doc_title, options.lang, options.max_chars, cache,
                                           shared_cache, sent["overviews"])
            except Exception as e:
                if is_connection_error(e):
                    raise FatalError(f"connection error with the LLM: {diagnose(e)}") from e
                errors += 1
                overview_error = f"{type(e).__name__}: {e}"[:500]
                log.warning("overview of '%s' failed: %s", doc_title, overview_error)

        cons = an.consolidate(sections)
        data = write_document(tmp, doc_title, path.name, llm.model if options.analysis == "llm" else "",
                              sections, doc_overview, cons,
                              extra={**metadata, "analysis_mode": options.analysis, "overview_error": overview_error,
                                     **{k: extracted[k] for k in ("pages", "pages_without_text") if k in extracted}})
        _replace_dir(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    status = {k: sum(1 for r in cons["requirements"] if r["status"] == k) for k in ("literal", "partial", "not_found")}
    log.info("'%s' done in %.0fs -> %s: %d sections (%d with errors), %d requirements %s, "
             "%d terms, %d entities, %d open issues", doc_title, time.time() - t0, out / "index.md",
             len(sections), errors, len(cons["requirements"]), status, len(cons["glossary"]),
             len(cons["entities"]), len(cons["open_issues"]))
    return {**data, "out_dir": str(out), "errors": errors, "structure_info": info,
            "sent": {k: len(v) for k, v in sent.items()}}


def _check(stop: threading.Event | None, doc_title: str) -> None:
    if stop is not None and stop.is_set():
        raise Interrupted(f"'{doc_title}': the run was interrupted")


def _analyze_all(llm: JsonLlm, sections: list[dict], doc_title: str, lang: str, cache: Path,
                 failures: Path, workers: int, stop: threading.Event | None = None, shared=None,
                 sent: list | None = None) -> int:
    """Analyses every section, in parallel, from the cache when possible. A section that
    fails keeps an empty analysis and its error; a connection error stops everything, and
    so does `stop`. `sent`, a list, gets the id of every section asked to the model."""
    cached = {s["id"] for s in sections
              if an.is_cached(an.section_cache_file(cache, llm.model, s, doc_title, lang), shared)}
    log.info("analysis: %d of %d sections already in the cache; %d to process",
             len(cached), len(sections), len(sections) - len(cached))
    t0, errors = time.time(), 0

    def one(s: dict) -> tuple[dict, float]:
        _check(stop, doc_title)
        if sent is not None and s["id"] not in cached:
            sent.append(s["id"])
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
