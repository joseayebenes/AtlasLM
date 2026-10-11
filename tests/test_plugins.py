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
