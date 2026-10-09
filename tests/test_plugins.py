import pytest
from atlas import plugins
from atlas.plugins import CodeAnalyzer, CodeUnit


def test_registro_y_descubrimiento():
    plugins.load_plugins()
    assert "database" in plugins.available("requirements")
    assert "text" in plugins.available("extractor")


def test_anadir_y_desactivar_un_analizador():
    class Cpp(CodeAnalyzer):
        name, extensions = "cpp", (".cpp", ".h")

        def analyze_file(self, path, source):
            return CodeUnit(file=str(path), unit=path.stem, language="cpp")

    plugins.register("code_analyzer", "cpp", Cpp)
    assert plugins.get("code_analyzer", "cpp") is Cpp
    with pytest.raises(LookupError, match="desactivado"):
        plugins.get("code_analyzer", "cpp", disabled=["code_analyzer:cpp"])
    with pytest.raises(LookupError, match="disponibles"):
        plugins.get("code_analyzer", "cobol")
