"""Comando doors-sync: combinacion de configuracion y comportamiento de extremo a extremo."""

import json

import pytest

from doors_kb.cli.sync_doors import _ajustes_desde, construir_parser, main
from doors_kb.db import SqliteRepository


def _argumentos(*argv):
    return construir_parser().parse_args(list(argv))


def test_los_argumentos_sobreescriben_el_entorno(monkeypatch):
    """El entorno define lo habitual; la linea de comandos, lo de esta ejecucion."""
    monkeypatch.setenv("DOORS_MODULE_PATH", "/Entorno/Modulo")
    monkeypatch.setenv("DOORS_SYNC_PAGE_SIZE", "25")

    ajustes = _ajustes_desde(_argumentos("--module", "/Cli/Modulo", "--page-size", "5"))

    assert ajustes.module_path == "/Cli/Modulo"
    assert ajustes.sync_page_size == 5


def test_sin_argumentos_se_conserva_lo_del_entorno(monkeypatch):
    monkeypatch.setenv("DOORS_MODULE_PATH", "/Entorno/Modulo")

    ajustes = _ajustes_desde(_argumentos())

    assert ajustes.module_path == "/Entorno/Modulo"
    assert ajustes.sync_page_size == 25


def test_la_lista_de_atributos_admite_espacios_alrededor_de_las_comas():
    ajustes = _ajustes_desde(_argumentos("--attributes", "Object Text , Estado"))

    assert ajustes.sync_attributes == ("Object Text", "Estado")


def test_la_sincronizacion_simulada_funciona_de_extremo_a_extremo(tmp_path, capsys):
    """Comprueba la instalacion completa sin necesitar DOORS."""
    base = tmp_path / "demo.sqlite3"

    codigo = main(["--source", "fake", "--module", "/Demo/Reqs", "--db", str(base)])

    assert codigo == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["inserted"] == 25
    assert stats["completed_module"] is True

    with SqliteRepository(base) as repo:
        assert repo.count_requirements("/Demo/Reqs") == 25
        assert repo.last_sync_run("/Demo/Reqs")["status"] == "success"


def test_dos_ejecuciones_seguidas_no_cambian_nada(tmp_path, capsys):
    """CA-002 comprobado a traves del comando, no solo de la API."""
    base = tmp_path / "demo.sqlite3"
    main(["--source", "fake", "--module", "/Demo/Reqs", "--db", str(base)])
    capsys.readouterr()

    main(["--source", "fake", "--module", "/Demo/Reqs", "--db", str(base)])

    stats = json.loads(capsys.readouterr().out)
    assert (stats["inserted"], stats["updated"], stats["unchanged"]) == (0, 0, 25)


def test_sin_modulo_el_comando_falla_con_un_mensaje_util(monkeypatch, capsys):
    """No se arranca una sincronizacion sin saber que modulo sincronizar (RF-005)."""
    monkeypatch.delenv("DOORS_MODULE_PATH", raising=False)

    codigo = main(["--source", "fake"])

    assert codigo == 1
    assert "DOORS_MODULE_PATH" in capsys.readouterr().err


def test_un_atributo_inexistente_devuelve_codigo_de_error(tmp_path, capsys):
    """CA-007 desde el comando: se rechaza antes de escribir nada."""
    base = tmp_path / "demo.sqlite3"

    codigo = main(
        ["--source", "fake", "--module", "/Demo/Reqs", "--db", str(base),
         "--attributes", "Object Text,No Existe"]
    )

    assert codigo == 1
    assert "Atributos inexistentes" in capsys.readouterr().err
    with SqliteRepository(base) as repo:
        assert repo.count_requirements("/Demo/Reqs", include_deleted=True) == 0


@pytest.mark.parametrize("bandera", ["--help"])
def test_la_ayuda_documenta_el_comando(bandera, capsys):
    with pytest.raises(SystemExit) as salida:
        main([bandera])

    assert salida.value.code == 0
    assert "doors-sync" in capsys.readouterr().out


def test_reconstruir_el_indice_no_necesita_doors(tmp_path, capsys):
    """RNF-016: el indice se rehace desde la copia local, sin abrir sesion Automation."""
    base = tmp_path / "demo.sqlite3"
    main(["--source", "fake", "--module", "/Demo/Reqs", "--db", str(base)])
    capsys.readouterr()

    with SqliteRepository(base) as repo:
        repo.conn.execute("DELETE FROM requirements_fts")
        assert repo.count_indexed("/Demo/Reqs") == 0

    # Sin --source doors y sin sesion: la reconstruccion es puramente local.
    codigo = main(["--rebuild-index", "--module", "/Demo/Reqs", "--db", str(base)])

    assert codigo == 0
    assert json.loads(capsys.readouterr().out)["indexed"] == 25
    with SqliteRepository(base) as repo:
        assert repo.count_indexed("/Demo/Reqs") == 25
