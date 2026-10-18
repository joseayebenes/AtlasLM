import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _streams_learned_per_test():
    """What atlas.llm.chat learns about streaming lasts the whole process: each test starts
    without it (the stubs of the client ignore stream)."""
    from atlas.llm import chat
    chat._STREAMS.clear()
    yield
    chat._STREAMS.clear()


@pytest.fixture(autouse=True)
def _no_log_handlers_left():
    """atlas.logs.setup_logging (each action of the CLI) puts on the root logger a handler of
    the stderr that the test had: it goes with that test, and the next one starts without it
    (a handler writing to a stderr already closed prints "Logging error" in the next test)."""
    yield
    import logging
    from atlas import logs
    root = logging.getLogger()
    for h in logs._STATE["handlers"]:
        root.removeHandler(h)
        h.close()
    logs._STATE.update(handlers=[], key=None)


@pytest.fixture(autouse=True, scope="session")
def _limits_dir(tmp_path_factory):
    """The windows that the limiter shares between processes (atlas.llm.connection.limits_dir)
    go to a temporary folder, never to ~/.atlas of whoever runs the tests."""
    os.environ["ATLAS_LIMITS_DIR"] = str(tmp_path_factory.mktemp("limits"))


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


@pytest.fixture(scope="session")
def fake_server(tmp_path_factory):
    """tests/fakes/fake_api.py on a free port, one for the whole session. Yields (base_url,
    control file)."""
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    control = tmp_path_factory.mktemp("fake") / "faults.json"; control.write_text("{}")
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
def fake(fake_server):
    """The fake API as each test finds it: no faults in the control file, the counters at
    zero (POST /reset) and nothing of another test learned by this process about it (they are
    shared by URL): no limiter (a rejection slows down or pauses the next requests), no window
    of the limit shared between processes, no tier of response_format.
    Yields (base_url, control file)."""
    from atlas.llm import connection, structured
    url, control = fake_server
    control.write_text("{}")
    urllib.request.urlopen(urllib.request.Request(url.removesuffix("/v1") + "/reset", data=b"", method="POST"))
    with connection._LIMITERS_LOCK:
        connection._LIMITERS.clear()
    with structured._TIERS_LOCK:
        structured._TIERS.clear()
    for window in connection.limits_dir().glob("*"):
        window.unlink(missing_ok=True)
    yield url, control


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
