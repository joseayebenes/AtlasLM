import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def samples():
    """Datos de ejemplo (se generan si no existen)."""
    from examples import make_samples
    d = RAIZ / "examples" / "samples"
    if not (d / "doors.sqlite").exists():
        make_samples.ada(); make_samples.doors()
    return d


@pytest.fixture
def toml(tmp_path, samples):
    """Escribe un atlas.toml de prueba en un directorio temporal."""
    def _hacer(extra: str = "") -> Path:
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
    return _hacer
