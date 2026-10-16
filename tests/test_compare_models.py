"""tools/compare_models.py: the table that compares models, from the eval reports that
atlas eval leaves in the workspace of each one."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _answer(case, passed, seconds, error=""):
    return {"id": case, "run": 1, "seconds": seconds, "passed": passed, **({"error": error} if error else {})}


def test_the_table_compares_the_reports_of_each_model(tmp_path):
    (tmp_path / "atlas.toml").write_text('[project]\nworkspace = "ws/${ATLAS_MODEL}"\n'
                                         '[agents.eval]\nsets = ["assistant.yaml", "retrieval.yaml"]\n')
    reports = tmp_path / "ws" / "google" / "gemma-4-26b-a4b-it" / "reports"
    reports.mkdir(parents=True)
    (reports / "eval-assistant.json").write_text(json.dumps({"agents": {"results": [
        _answer("trace", True, 4), _answer("impact", False, 12), _answer("days", False, 30, "APIStatusError: 402")]}}))
    (reports / "eval-retrieval.json").write_text(json.dumps({"retrieval": {"search": {}}}))   # no agents
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "compare_models.py"), "google/gemma-4-26b-a4b-it",
                          "qwen/qwen3.6-35b-a3b", "--config", str(tmp_path / "atlas.toml"), "--no-run",
                          "--out", str(tmp_path / "comparison.md")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "| `google/gemma-4-26b-a4b-it` | 1/3 | 12 | 46 | 1 | days, impact |" in res.stdout
    assert "| `qwen/qwen3.6-35b-a3b` | no report |" in res.stdout
    assert (tmp_path / "comparison.md").read_text().startswith("| model | right |")
