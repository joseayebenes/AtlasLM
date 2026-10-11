"""Phase 2 of the plan on a small copy of the large synthetic project (examples/large_project.py,
which also measures the full one): a second run without changes makes no request to the
API, and a PDF changed in one page only sends the section of that page to the model again."""
import contextlib
import io
import json

import pytest

from atlas.cli import run
from atlas.config import load_config


def test_second_run_and_a_pdf_changed_in_one_page(tmp_path, fake, fake_stats):
    for module in ("pymupdf", "markitdown", "tree_sitter_ada"):
        pytest.importorskip(module)
    from examples.large_project import generate, per_pdf, spec_pdf
    sizes = generate(tmp_path, fake[0], pdfs=2, units=20, per_unit=4, tests=5, sss=20, srs=200)
    cfg = load_config(tmp_path / "atlas.toml")

    def go():
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert run(cfg) == 0
        return json.loads(out.getvalue())

    assert go()["processed"] == {"doors.requirements": 2, "code.parse": 45, "docs.process": 2, "graph.build": 3,
                                 "index.documents": 2, "index.requirements": 2, "index.code": 25}
    before = fake_stats()
    assert go()["processed"] == {} and fake_stats() == before      # nothing changed: no request at all

    first = 1 + per_pdf(sizes["srs"], sizes["pdfs"])
    spec_pdf(tmp_path / "docs" / "spec_01.pdf", 1, first, per_pdf(sizes["srs"], sizes["pdfs"]), sizes["units"],
             override={f"SRS-{first + 7:05d}": 999})
    assert go()["processed"] == {"docs.process": 1, "graph.build": 1, "index.documents": 1}
    after = fake_stats()
    # only the changed section goes to the model (the overview is cached: the summaries it is
    # made from did not change) and only the records whose text changed are embedded
    assert after["chat"] - before["chat"] == 1
    assert 0 < after["emb_texts"] - before["emb_texts"] <= 4, after["emb_texts"] - before["emb_texts"]
