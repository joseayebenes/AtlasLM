import json
from atlas.cli import run, status
from atlas.config import load_config


def test_run_incremental_con_doors_y_codigo(toml, samples, capsys):
    cfg = load_config(toml(f'''
[pipeline]
steps = ["doors", "code"]
[sources.code]
roots = ["{(samples / 'ada').as_posix()}"]
languages = ["ada"]
'''))
    import pytest
    pytest.importorskip("tree_sitter_ada")
    assert run(cfg) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["processed"]["doors.requirements"] == 3 and rep["processed"]["code.parse"] >= 4
    assert run(cfg) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["processed"] == {} and rep["skipped"]["code.parse"] >= 4
    assert status(cfg) == 0
