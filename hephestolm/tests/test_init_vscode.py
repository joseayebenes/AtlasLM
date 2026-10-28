"""hephesto init --vscode: its server beside that of atlas in .vscode/mcp.json."""
import json

import pytest

from hephesto.cli import main


def test_hephesto_init_vscode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="atlas init --vscode atlas.toml"):
        main(["init", "--vscode"])                            # no atlas.toml yet
    (tmp_path / "atlas.toml").write_text("")
    with pytest.raises(SystemExit) as end:
        main(["init", "--vscode", "--edit"])
    assert end.value.code == 0
    entry = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())["servers"]["hephesto"]
    assert entry["args"] == ["-m", "hephesto", "serve", "atlas.toml", "--edit"]
