"""The reference manuals of the language and of the compiler, and the declarations of the
library, searchable by the agents without a model: the tool search_reference.

    ref = Reference(cfg.workspace / "reference.db", ["~/.local/share/alire/toolchains/gnat_native_*/share/info/gnat_rm.info"], library)
    ref.search("Unbounded_String slice")       # passages, the best first

Manuals, by extension: GNU info (.info, the format of the manuals that come with GNAT: a node
is a section), Markdown or text (.md, .txt: a heading starts a section) and HTML (.html, .htm,
without tags). Each section is cut in pieces of about 1200 characters. The library: a record
per public declaration of each spec of [sources.code] library, with its comment
("Ada.Strings.Fixed.Index: function Index (...)"), to find which package has something.

They go in their own SQLite file, <workspace>/reference.db, not in atlas.db: the search of the
documents of the project does not get the manuals mixed in. Built on the first search, and a
manual (or the library) again when it changes: each search looks at their dates and sizes, a
few milliseconds. Lexical search (BM25 over the same FTS5 tables
of atlas.index), with no model and no embeddings.
"""
from __future__ import annotations

import glob
import hashlib
import html
import os
import re
import threading
from pathlib import Path
from typing import Iterable

import numpy as np

from atlas.code.library import Library, _read
from atlas.index.records import chunk_text
from atlas.index.search import rrf
from atlas.index.store import IndexStore

__all__ = ["Reference", "MANUALS"]

MANUALS = (".info", ".md", ".txt", ".html", ".htm")
MAX_PASSAGE = 900          # characters of a passage in an answer
LIBRARY = "library"        # the key of the library in the index


def _sections_info(text: str) -> list[tuple[str, str]]:
    """The nodes of a GNU info file: (node name, its text), without menus and underlines."""
    out = []
    for node in text.split("\x1f"):
        node = node.strip("\n")
        if not node.startswith("File:"):
            continue                                        # the tag table, the indirect table
        header, _, body = node.partition("\n")
        m = re.search(r"Node:\s*([^,\n]+)", header)
        body = re.sub(r"(?m)^\* .*::.*$|^[=*\-]{3,}\s*$", "", body)
        out.append(((m.group(1).strip() if m else ""), body.strip()))
    return out


def _sections_text(text: str) -> list[tuple[str, str]]:
    """The sections of Markdown or plain text: a heading (# Title) starts one."""
    out, title, buf = [], "", []
    for line in text.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            if "".join(buf).strip():
                out.append((title, "\n".join(buf).strip()))
            title, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        out.append((title, "\n".join(buf).strip()))
    return out


def _sections_html(text: str) -> list[tuple[str, str]]:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text)
    text = re.sub(r"(?is)<h[1-3][^>]*>(.*?)</h[1-3]>", lambda m: "\n# " + re.sub(r"<[^>]+>", "", m.group(1)) + "\n",
                  text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</tr>", "\n", text)
    return _sections_text(html.unescape(re.sub(r"<[^>]+>", "", text)))


def manual_records(path: Path) -> list[dict]:
    """The records of a manual: a piece of a section each."""
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    sections = _sections_info(text) if suffix == ".info" else _sections_html(text) if suffix in (".html", ".htm") \
        else _sections_text(text)
    records = []
    for title, body in sections:
        for piece in chunk_text(body):
            records.append({"kind": "manual", "section_title": title, "ref": title, "seq": len(records),
                            "text": piece, "embed_text": f"{title}\n{piece}"})
    return records


def library_records(library: Library) -> list[dict]:
    """A record per public declaration of the specs of the library folders, but those of the
    internal units of GNAT (System.*), which a program is not meant to use."""
    records = []
    for path in (p for f in library.folders if f.is_dir() for p in sorted(f.glob("*.ads"))):
        data = _read(path)
        if data["unit"].lower().startswith("system."):
            continue
        for d in data["declarations"]:
            ref = f"{data['unit']}.{d['name']}"
            text = d["text"] + (f"\n{d['doc']}" if d.get("doc") else "")
            records.append({"kind": "spec", "section_title": ref, "ref": ref, "seq": len(records), "text": text,
                            "embed_text": f"{ref}\n{text}"})
    return records


def _fingerprint(paths: Iterable[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        st = p.stat()
        h.update(f"{p}|{st.st_mtime_ns}|{st.st_size}\n".encode())
    return h.hexdigest()[:16]


class Reference:
    def __init__(self, db: str | Path, sources: Iterable[str] = (), library: Library | None = None):
        self.db = Path(db)
        self.patterns = list(sources)
        self.library = library
        self._store: IndexStore | None = None
        self._lock = threading.Lock()

    def manuals(self) -> list[Path]:
        """The files of the manuals: the patterns, a folder being every manual in it."""
        out = []
        for pattern in self.patterns:
            for p in sorted(glob.glob(os.path.expanduser(pattern))):
                p = Path(p)
                found = sorted(f for f in p.rglob("*") if f.suffix.lower() in MANUALS) if p.is_dir() else [p]
                out += [f for f in found if f.is_file() and f not in out]
        return out

    def _library_files(self) -> list[Path]:
        if self.library is None:
            return []
        return [p for f in self.library.folders if f.is_dir() for p in sorted(f.glob("*.ads"))]

    def ensure(self) -> dict:
        """Builds what is missing or changed; returns {name: records} of what is indexed."""
        with self._lock:
            if self._store is None:
                self._store = IndexStore(self.db)
            store = self._store
            indexed = {d["doc"]: d for d in store.documents()}
            wanted: dict[str, tuple[str, str, list[Path]]] = {
                f"manual:{p}": (p.name, "manual", [p]) for p in self.manuals()}
            specs = self._library_files()
            if specs:
                wanted[LIBRARY] = ("library", "library", specs)
            for doc, (name, kind, paths) in wanted.items():
                fingerprint = _fingerprint(paths)
                if indexed.get(doc, {}).get("overview", {}).get("fingerprint") == fingerprint:
                    continue
                records = manual_records(paths[0]) if kind == "manual" else library_records(self.library)
                store.replace_document({"doc": doc, "name": name, "title": name, "embed_model": "",
                                        "overview": {"fingerprint": fingerprint, "files": len(paths)}},
                                       records, np.zeros((len(records), 1), np.float32))
            for doc in set(indexed) - set(wanted):        # a manual taken out of the configuration
                store.delete_document(doc)
            return {d["name"]: d["records"] for d in store.documents()}

    def search(self, query: str, k: int = 5) -> list[dict]:
        """The k best passages for `query`: {"source", "text"}, the best first."""
        self.ensure()
        store, k = self._store, max(1, min(k, 20))
        ids = rrf([r for r in (store.by_ref(query, None), store.lexical(query, k * 4, None)) if r])[:k]
        out = []
        for r in store.records(ids):
            source = r["ref"] if r["kind"] == "spec" else f"{r['doc_name']}: {r['section_title']}"
            text = r["text"] if len(r["text"]) <= MAX_PASSAGE else r["text"][:MAX_PASSAGE - 1] + "…"
            out.append({"source": source, "text": text})
        return out
