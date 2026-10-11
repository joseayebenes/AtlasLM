#!/usr/bin/env python3
"""atlas.docs on its own, without a model: the sections of the sample specification and
its requirements found with a regular expression.

    python examples/library/documents.py [OUTDIR]
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.docs import DocsOptions, preview, process_document  # noqa: E402

SPEC = ROOT / "examples" / "samples" / "docs" / "spec_with_bookmarks.pdf"


def main(outdir: Path) -> None:
    if not SPEC.exists():
        sys.exit("first: python examples/make_samples.py")
    sections = preview(SPEC)                                # what the document would give
    print(f"{sections['title']}: {len(sections['sections'])} sections from {sections['structure']}")
    data = process_document(SPEC, outdir / "spec", DocsOptions(analysis="regex"))
    first = data["requirements"][0]
    print(f"{len(data['requirements'])} requirements, e.g. {first['id']}: {first['text']}")
    print(f"Markdown in {data['out_dir']}/index.md")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="atlas-documents-")))
