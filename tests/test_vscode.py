"""atlas init --vscode: the server of atlas in .vscode/mcp.json, next to atlas.toml."""
import json
import sys

import pytest

from atlas.cli import main
from atlas.vscode import add_server


def test_the_server_goes_next_to_atlas_toml_and_keeps_the_others(tmp_path):
    config = tmp_path / "atlas.toml"
    config.write_text("")
    path, done = add_server(config, "atlas")
    assert (path, done) == (tmp_path / ".vscode" / "mcp.json", "created")
    entry = json.loads(path.read_text())["servers"]["atlas"]
    assert entry == {"type": "stdio", "command": sys.executable, "args": ["-m", "atlas", "serve", "atlas.toml"],
                     "cwd": "${workspaceFolder}"}
    assert add_server(config, "atlas")[1] == "unchanged"
    data = json.loads(path.read_text())
    data["servers"]["other"] = {"type": "http", "url": "http://x"}
    data["inputs"] = []
    data["servers"]["atlas"]["args"] = ["serve"]
    path.write_text(json.dumps(data))
    assert add_server(config, "atlas")[1] == "updated"
    assert add_server(config, "hephesto", args=("--edit",))[1] == "added"
    data = json.loads(path.read_text())
    assert list(data["servers"]) == ["atlas", "other", "hephesto"] and data["inputs"] == []
    assert data["servers"]["atlas"]["args"][-1] == "atlas.toml"
    assert data["servers"]["hephesto"]["args"] == ["-m", "hephesto", "serve", "atlas.toml", "--edit"]
    path.write_text('{\n  // a comment\n  "servers": {}\n}\n')            # JSONC: not touched
    with pytest.raises(ValueError, match="not plain JSON.*by hand"):
        add_server(config, "atlas")
    assert "// a comment" in path.read_text()


def test_atlas_init_vscode(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as end:
        main(["init", "--vscode"])                            # atlas.toml and the server
    assert end.value.code == 0 and (tmp_path / "atlas.toml").exists()
    assert "the server atlas created" in capsys.readouterr().out
    with pytest.raises(SystemExit) as end:
        main(["init", "--vscode"])                            # atlas.toml exists: only the server
    assert end.value.code == 0 and "the server atlas unchanged" in capsys.readouterr().out
    with pytest.raises(SystemExit) as end:
        main(["init"])                                        # without --vscode, as always
    assert end.value.code == 1
