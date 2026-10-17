"""tools/compare_models.py: the table that compares models, from the eval reports that
atlas eval leaves in the workspace of each one."""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _answer(case, passed, seconds, tokens=0, cost=None, error=""):
    return {"id": case, "run": 1, "seconds": seconds, "passed": passed,
            "tokens": {"prompt": tokens - tokens // 10, "completion": tokens // 10},
            **({"cost": cost} if cost is not None else {}), **({"error": error} if error else {})}


def test_the_table_compares_the_reports_of_each_model(tmp_path):
    (tmp_path / "atlas.toml").write_text('[project]\nworkspace = "ws/${ATLAS_MODEL}"\n'
                                         '[agents.eval]\nsets = ["assistant.yaml", "retrieval.yaml"]\n')
    reports = tmp_path / "ws" / "google" / "gemma-4-26b-a4b-it" / "reports"
    reports.mkdir(parents=True)
    (reports / "eval-assistant.json").write_text(json.dumps({"agents": {"results": [
        _answer("trace", True, 4, 110, 0.001), _answer("impact", False, 12, 220, 0.002),
        _answer("days", False, 30, error="APIStatusError: 402")]}}))
    (reports / "eval-retrieval.json").write_text(json.dumps({"retrieval": {"search": {   # no agents
        "cases": 20, "hybrid": {"hit@3": 0.9, "mrr": 0.84}, "lexical": {"hit@3": 0.7, "mrr": 0.68}}}}))
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "compare_models.py"), "google/gemma-4-26b-a4b-it",
                          "qwen/qwen3.6-35b-a3b", "--config", str(tmp_path / "atlas.toml"), "--no-run",
                          "--out", str(tmp_path / "comparison.md")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    # tokens per case: the mean of 110, 220 and 0; $ per right: the only right one costs it all
    assert "| `google/gemma-4-26b-a4b-it` | 1/3 | 12 | 46 | 110 | 0.0030 | 0.0030 | 90% | 0.84 | 1 | days, impact |" \
        in res.stdout
    assert "| `qwen/qwen3.6-35b-a3b` | no report |" in res.stdout
    assert (tmp_path / "comparison.md").read_text().startswith("| model | right |")
    stem = tmp_path / "reports" / f"compare-{date.today().isoformat()}"
    assert stem.with_suffix(".md").read_text() == (tmp_path / "comparison.md").read_text()
    models = json.loads(stem.with_suffix(".json").read_text())["models"]
    assert [m["model"] for m in models] == ["google/gemma-4-26b-a4b-it", "qwen/qwen3.6-35b-a3b"]
    assert models[0]["cases"][1] == {"id": "impact", "run": 1, "passed": False, "seconds": 12,
                                     "tokens": {"prompt": 198, "completion": 22}, "cost": 0.002}
    assert models[0]["cost_per_right"] == 0.003 and models[1]["answers"] == 0
