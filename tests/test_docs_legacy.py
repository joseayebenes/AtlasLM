"""atlas.docs gives the same result as legacy/pdf_struct.py on the sample documents, with
the fake API as the model. To be deleted together with legacy/."""
import json
import sys
from pathlib import Path

import pytest

from atlas.docs import DocsOptions, process_document

LEGACY = Path(__file__).resolve().parents[1] / "legacy"
pytestmark = pytest.mark.skipif(not (LEGACY / "pdf_struct.py").exists(), reason="legacy/ has been removed")

# what the migration translated into English
WORDS = {"Preámbulo": "Preamble", "(parte ": "(part ", "Bloque ": "Block ",
         '"parcial"': '"partial"', '"no_encontrado"': '"not_found"'}
KEYS = ("title", "overview", "glossary", "entities", "requirements", "open_issues")
SECTION_KEYS = ("id", "title", "path", "includes", "page_start", "page_end", "analysis", "body")


def _english(data: dict) -> dict:
    text = json.dumps(data, ensure_ascii=False)
    for old, new in WORDS.items():
        text = text.replace(old, new)
    return json.loads(text)


@pytest.fixture(scope="module")
def legacy():
    pytest.importorskip("markitdown")
    sys.path.insert(0, str(LEGACY))
    try:
        import pdf_struct
        import conexion
        # the one intended difference: clean_headers no longer removes body lines that
        # follow a template when they fall at the edge of a page
        from atlas.docs.structure import clean_headers
        original, pdf_struct.limpiar_cabeceras = pdf_struct.limpiar_cabeceras, clean_headers
        yield pdf_struct, conexion
        pdf_struct.limpiar_cabeceras = original
    finally:
        sys.path.remove(str(LEGACY))


@pytest.mark.slow
@pytest.mark.parametrize("name", ["spec_with_bookmarks.pdf", "spec_without_bookmarks.pdf", "hmi_manual.md"])
def test_same_result_as_pdf_struct(name, legacy, docs_samples, tmp_path, fake):
    ps, cx = legacy
    url, _ = fake
    net = dict(proxy="", no_proxy="*")
    old = ps.procesar(docs_samples / name, tmp_path / "old", lang="Spanish", min_chars=1000,
                      llm_cfg=cx.ServiceConfig(base_url=url, model="fake", api_key="x", net=cx.NetConfig(**net)))
    from atlas.llm import NetConfig, ServiceConfig
    # pdf_struct.py reads PDFs with markitdown: the comparison is of what was migrated, with the same text
    new = process_document(docs_samples / name, tmp_path / "new", DocsOptions(lang="Spanish", min_chars=1000),
                           llm=ServiceConfig(base_url=url, model="fake", api_key="x", net=NetConfig(**net)),
                           disabled=["extractor:pymupdf"])
    old = _english(old)
    assert {k: new[k] for k in KEYS} == {k: old[k] for k in KEYS}
    assert [{k: s[k] for k in SECTION_KEYS} for s in new["sections"]] == \
           [{k: s[k] for k in SECTION_KEYS} for s in old["sections"]]
    assert len(new["requirements"]) > 0
