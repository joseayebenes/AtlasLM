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
    if not (d / "doors.sqlite").exists():
        make_samples.ada(); make_samples.doors()
    return d


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
