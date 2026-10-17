import pytest
from atlas import plugins
from atlas.plugins import CodeAnalyzer, CodeUnit


def test_registry_and_discovery():
    plugins.load_plugins()
    assert "database" in plugins.available("requirements")
    assert "text" in plugins.available("extractor")


def test_the_extractor_with_the_highest_priority_reads_an_extension():
    from atlas.docs import extractor_for
    assert extractor_for("spec.pdf").name == "pymupdf"
    assert extractor_for("spec.pdf", disabled=["extractor:pymupdf"]).name == "markitdown"
    assert extractor_for("plan.docx").name == "markitdown" and extractor_for("notes.md").name == "text"
    with pytest.raises(LookupError, match="no enabled extractor"):
        extractor_for("spec.pdf", disabled=["extractor:pymupdf", "extractor:markitdown"])


def test_add_and_disable_an_analyzer():
    class Cpp(CodeAnalyzer):
        name, extensions = "cpp", (".cpp", ".h")

        def analyze_file(self, path, source):
            return CodeUnit(file=str(path), unit=path.stem, language="cpp")

    plugins.register("code_analyzer", "cpp", Cpp)
    assert plugins.get("code_analyzer", "cpp") is Cpp
    with pytest.raises(LookupError, match="disabled"):
        plugins.get("code_analyzer", "cpp", disabled=["code_analyzer:cpp"])
    with pytest.raises(LookupError, match="available"):
        plugins.get("code_analyzer", "cobol")


def test_instrumented_keeps_the_signature_and_logs_each_call(tmp_path):
    """One wrapper for the tools of the MCP server, of the agents and the commands: the
    signature and the docstring stay (ADK and FastMCP read them), a call that raises is
    logged and raised, and a sink that fails does not fail the call."""
    import inspect
    from atlas.plugins import instrumented
    events = []

    def search(query: str, k: int = 0, cfg: object = None) -> dict:
        """Finds things."""
        if query == "boom":
            raise RuntimeError("down")
        return {"results": [query] * k}

    wrapped = instrumented(search, events.append, origin_hint="test")
    assert wrapped.__name__ == "search" and wrapped.__doc__ == "Finds things."
    assert list(inspect.signature(wrapped).parameters) == ["query", "k", "cfg"]
    assert wrapped("alarm", k=2, cfg=object()) == {"results": ["alarm", "alarm"]}
    with pytest.raises(RuntimeError):
        wrapped("boom")
    first, second = events
    assert first["name"] == "search" and first["args"] == {"query": "alarm", "k": 2}    # cfg is not plain
    assert first["error"] is None and first["seconds"] >= 0 and first["origin_hint"] == "test"
    assert second["error"] == "RuntimeError: down" and second["result"] is None

    def broken(event):
        raise OSError("disk full")
    assert instrumented(search, broken)("x", k=1) == {"results": ["x"]}


def test_the_usage_log_keeps_counts_or_cut_arguments(tmp_path):
    import sqlite3
    from atlas.plugins import instrumented
    from atlas.usage import UsageLog, answer_status, parse_since, summary
    db = tmp_path / "atlas.db"

    def ask_docs(question: str) -> dict:
        return {"answer": "x", "sources": [] if "nothing" in question else [{"n": 1}]}

    def trace(req_id: str) -> dict:
        return {"error": f"requirement '{req_id}' not found"}

    counts = UsageLog(db, "counts", "mcp")
    instrumented(ask_docs, counts)(question="When is an alarm raised?")
    instrumented(ask_docs, counts)(question="nothing about this")
    instrumented(trace, counts)(req_id="SRS-9")
    instrumented(ask_docs, UsageLog(db, "full", "cli"), name="ask")(question="Why? " * 100)
    rows = sqlite3.connect(db).execute("SELECT origin, name, outcome, status, args FROM usage ORDER BY id").fetchall()
    assert rows[:3] == [("mcp", "ask_docs", "ok", "answered", None), ("mcp", "ask_docs", "ok", "not_found", None),
                        ("mcp", "trace", "error", "error", None)]
    assert rows[3][:4] == ("cli", "ask", "ok", "answered") and rows[3][4].startswith('{"question": "Why? Why?')
    assert len(rows[3][4]) == 200                                               # full: cut at 200 characters
    res = summary(db, 30)
    assert res["calls"] == 4 and res["answers"] == {"ask_docs": {"answered": 1, "not_found": 1}, "ask": {"answered": 1}}
    by = {(r["origin"], r["name"]): r for r in res["by_name"]}
    assert by[("mcp", "ask_docs")]["calls"] == 2 and by[("mcp", "trace")]["errors"] == 1
    assert answer_status({"status": "partial", "sources": []}) == "partial" and answer_status([1]) is None
    assert (parse_since("30d"), parse_since("2w"), parse_since("1m"), parse_since("7")) == (30, 14, 30, 7)
    with pytest.raises(ValueError, match="--since"):
        parse_since("soon")
    with pytest.raises(ValueError):
        UsageLog(db, "everything")
