import pytest
from atlas import plugins
from atlas.plugins import CodeAnalyzer, CodeUnit


def test_registry_and_discovery():
    plugins.load_plugins()
    assert "database" in plugins.available("requirements")
    assert "text" in plugins.available("extractor")


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
