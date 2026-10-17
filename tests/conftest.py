import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def samples():
    """Sample data (generated if missing)."""
    from examples import make_samples
    d = ROOT / "examples" / "samples"
    if not (d / "requirements.sqlite").exists():
        make_samples.ada(); make_samples.doors(); make_samples.exchange()
    return d


@pytest.fixture(scope="session")
def docs_samples(samples):
    """examples/samples/docs (the PDFs need pymupdf; reading them, markitdown)."""
    from examples import make_samples
    d = samples / "docs"
    if not (d / "spec_without_bookmarks.pdf").exists():
        pytest.importorskip("pymupdf")
        make_samples.pdfs()
    return d


@pytest.fixture(scope="session")
def scanned_pdf(samples):
    """examples/samples/scanned/scanned_page.pdf: one page that is only an image."""
    from examples import make_samples
    f = samples / "scanned" / "scanned_page.pdf"
    if not f.exists():
        pytest.importorskip("pymupdf")
        make_samples.scanned()
    return f


@pytest.fixture(scope="session")
def tables_pdf(samples):
    """examples/samples/tables/interface_tables.pdf: requirements in a table with borders."""
    from examples import make_samples
    f = samples / "tables" / "interface_tables.pdf"
    if not f.exists():
        pytest.importorskip("pymupdf")
        make_samples.tables()
    return f


@pytest.fixture
def toml(tmp_path, samples):
    """Writes a test atlas.toml in a temporary directory."""
    def _make(extra: str = "") -> Path:
        p = tmp_path / "atlas.toml"
        p.write_text(f'''
[project]
name = "t"
workspace = "{(tmp_path / 'ws').as_posix()}"
[sources.doors]
adapter = "database"
[sources.doors.database]
driver = "sqlite"
path = "{(samples / 'doors.sqlite').as_posix()}"
modules_query = "SELECT DISTINCT module FROM requirements ORDER BY module"
requirements_query = "SELECT * FROM requirements WHERE module = :module ORDER BY id"
links_query = "SELECT source_id, target_id, link_type FROM links WHERE source_module = :module"
snapshot_query = "SELECT COUNT(*) || '|' || MAX(updated_at) FROM requirements WHERE module = :module"
[sources.doors.convention]
parent_column = "parent"
attribute_columns = ["status"]
id_pattern = "^(?:SRS|SSS)-\\\\d{{4}}$"
inline_reference_regex = "\\\\b(?:SRS|SSS)-\\\\d{{4}}\\\\b"
{extra}
''', encoding="utf-8")
        return p
    return _make


@pytest.fixture
def fake(tmp_path):
    """tests/fakes/fake_api.py on a free port. Yields (base_url, control file)."""
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    control = tmp_path / "faults.json"; control.write_text("{}")
    proc = subprocess.Popen([sys.executable, str(Path(__file__).parent / "fakes" / "fake_api.py"),
                             "--port", str(port), "--control", str(control)])
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2); break
        except Exception:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/v1", control
    proc.terminate(); proc.wait(timeout=5)


@pytest.fixture
def fake_llm(fake):
    """ServiceConfig pointing at the fake API, and its control file."""
    from atlas.llm import NetConfig, ServiceConfig
    url, control = fake
    return ServiceConfig(base_url=url, model="fake", api_key="x", net=NetConfig(proxy="", no_proxy="*")), control


@pytest.fixture
def fake_stats(fake):
    """Function that returns the counters of the fake API (chat, embeddings, emb_texts)."""
    import json
    url = fake[0].removesuffix("/v1") + "/"
    return lambda: json.load(urllib.request.urlopen(url))


@pytest.fixture
def fake_chats(fake_stats):
    """Function that returns how many chat requests the fake API has received."""
    return lambda: fake_stats()["chat"]


@pytest.fixture
def fake_embeddings(fake):
    """ServiceConfig of the embeddings of the fake API."""
    from atlas.llm import NetConfig, ServiceConfig
    return ServiceConfig(base_url=fake[0], model="embeddinggemma", api_key="x", batch=16,
                         net=NetConfig(proxy="", no_proxy="*"))
