"""Servidor MCP directo: catalogo, seguridad y limites de respuesta.

Cubre el catalogo de la seccion 8 y las reglas de la seccion 3.5 (RF-040..RF-044, RNF-009,
RNF-010). Se prueba contra la fuente falsa, sin DOORS.
"""

import asyncio
import json

import pytest

from doors_kb.config import Settings
from doors_kb.errors import ResponseTooLargeError
from doors_kb.servers import response
from doors_kb.servers.doors_server import crear_servidor

MODULO = "/Demo/Reqs"


class ClienteFalso:
    """Adapta ``FakeDoorsSource`` a lo que espera el servidor: le anade sesion y estado."""

    def __init__(self, fuente) -> None:
        self.fuente = fuente
        self.sesion_iniciada = False

    def start_session(self, timeout=None):
        self.sesion_iniciada = True
        return {"status": "started", "prog_id": "DOORS.Application"}

    def status(self, module_path=None):
        return {"session": "ready" if self.sesion_iniciada else "not_started"}

    def __getattr__(self, nombre):
        return getattr(self.fuente, nombre)


@pytest.fixture
def servidor(fuente, atributos):
    ajustes = Settings(module_path=MODULO, sync_attributes=atributos)
    return crear_servidor(ajustes, ClienteFalso(fuente))


def llamar(servidor, herramienta, **argumentos) -> dict:
    """Invoca una tool y devuelve su JSON ya parseado."""
    resultado = asyncio.run(servidor.call_tool(herramienta, argumentos))
    return json.loads(resultado.content[0].text)


# ---------------------------------------------------------------------------------------
# Catalogo y seguridad
# ---------------------------------------------------------------------------------------


def test_el_catalogo_es_el_de_la_especificacion(servidor):
    """Seccion 8: las nueve herramientas implementadas del hito H1."""
    nombres = {t.name for t in asyncio.run(servidor.list_tools())}

    assert nombres == {
        "doors_configuration",
        "start_doors_session",
        "doors_status",
        "list_object_attributes",
        "validate_attributes",
        "list_requirements",
        "get_requirement",
        "search_requirements",
        "get_requirement_links",
    }


def test_no_existe_ninguna_herramienta_de_dxl_arbitrario(servidor):
    """RF-040 y ADR-003: un agente no puede ejecutar DXL libre contra la base de requisitos."""
    nombres = {t.name for t in asyncio.run(servidor.list_tools())}

    assert not {n for n in nombres if "dxl" in n or "run" in n or "exec" in n}


def test_todas_las_herramientas_se_declaran_de_solo_lectura(servidor):
    """RF-042: la anotacion permite al cliente MCP saber que ninguna modifica nada."""
    for herramienta in asyncio.run(servidor.list_tools()):
        assert herramienta.annotations is not None
        assert herramienta.annotations.read_only_hint is True
        assert herramienta.annotations.destructive_hint is False


# ---------------------------------------------------------------------------------------
# Comportamiento de las herramientas
# ---------------------------------------------------------------------------------------


def test_la_configuracion_no_expone_secretos(servidor):
    datos = llamar(servidor, "doors_configuration")["configuration"]

    assert datos["prog_id"] == "DOORS.Application"
    assert datos["hard_max_response_chars"] == 500_000
    assert not any("password" in c or "token" in c for c in datos)


def test_listar_requisitos_devuelve_cursor_para_continuar(servidor):
    """RF-020 y ADR-006: el recorrido se continua con next_cursor, no con offset."""
    primera = llamar(servidor, "list_requirements", limit=2)

    assert [r["absolute_number"] for r in primera["records"]] == [1, 2]
    assert primera["next_cursor"] == 2

    segunda = llamar(servidor, "list_requirements", limit=2, cursor=primera["next_cursor"])

    assert [r["absolute_number"] for r in segunda["records"]] == [3]
    assert segunda["next_cursor"] is None  # final del modulo


def test_un_requisito_incluye_los_campos_minimos(servidor):
    """RF-025: Absolute Number, identificador, outline number y atributos pedidos."""
    registro = llamar(servidor, "get_requirement", absolute_number=2)["record"]

    assert registro["absolute_number"] == 2
    assert registro["identifier"] == "REQ-2"
    assert registro["outline_number"] == "2"
    assert registro["heading"] == "Timeout TCP"
    assert registro["attributes"] == {"Estado": "Aprobado"}


def test_un_requisito_inexistente_no_es_un_error(servidor):
    """Que no exista es una respuesta valida; el error se reserva a los fallos reales."""
    respuesta = llamar(servidor, "get_requirement", absolute_number=999)

    assert respuesta["record"] is None
    assert "borrado" in respuesta["message"]


def test_la_busqueda_indica_donde_encontro_la_coincidencia(servidor):
    """RF-034."""
    respuesta = llamar(servidor, "search_requirements", query="30 segundos")

    assert len(respuesta["hits"]) == 1
    assert respuesta["hits"][0]["match"]["attribute"] == "Object Text"
    assert respuesta["hits"][0]["match"]["text"] == "30 segundos"


def test_un_atributo_mal_escrito_devuelve_un_error_con_sugerencias(servidor):
    """RF-013: el agente puede corregirse solo en lugar de quedarse sin datos."""
    respuesta = llamar(servidor, "list_requirements", attributes=["Object Txt"])

    assert respuesta["error"] == "AttributeValidationError"
    assert "Object Text" in respuesta["message"]


def test_validate_attributes_devuelve_el_informe_en_lugar_de_fallar(servidor):
    """RF-012: sirve para comprobar antes de lanzar una consulta cara."""
    informe = llamar(
        servidor, "validate_attributes", attributes=["Object Text", "Estdo"]
    )["validation"]

    assert informe["ok"] is False
    assert [v["name"] for v in informe["valid"]] == ["Object Text"]
    assert informe["unknown"][0]["suggestions"] == ["Estado"]


def test_la_trazabilidad_declara_que_no_cubre_oslc(servidor):
    """RF-037."""
    respuesta = llamar(servidor, "get_requirement_links", absolute_number=1)

    assert respuesta["oslc_links_included"] is False


def test_sin_modulo_configurado_el_error_dice_que_falta(servidor, fuente, atributos):
    """RF-005: o se pasa module_path, o se define DOORS_MODULE_PATH."""
    vacio = crear_servidor(Settings(sync_attributes=atributos), ClienteFalso(fuente))

    respuesta = llamar(vacio, "list_requirements")

    assert respuesta["error"] == "ConfigurationError"
    assert "DOORS_MODULE_PATH" in respuesta["message"]


# ---------------------------------------------------------------------------------------
# Limite de tamano de la respuesta (RF-043, RF-044)
# ---------------------------------------------------------------------------------------


def test_una_respuesta_que_cabe_no_se_toca():
    datos = {"records": [{"absolute_number": 1}]}

    assert "truncated" not in json.loads(response.serializar(datos, 100_000))


def test_una_respuesta_demasiado_grande_se_recorta_y_lo_dice():
    """RF-044: el agente debe distinguir 'no hay mas' de 'hay mas, pero no caben'."""
    datos = {"records": [{"absolute_number": n, "text": "x" * 500} for n in range(50)]}

    recortado = json.loads(response.serializar(datos, 5_000))

    assert len(recortado["records"]) < 50
    assert recortado["truncated"]["omitted"] > 0
    assert "Reduce 'limit'" in recortado["truncated"]["hint"]


def test_si_ni_recortando_cabe_se_devuelve_un_error_de_tamano():
    """El caso extremo: un solo requisito enorme. Callarlo daria una respuesta enganosa."""
    datos = {"records": [{"absolute_number": 1, "text": "x" * 10_000}]}

    with pytest.raises(ResponseTooLargeError, match="Reduce 'limit'"):
        response.serializar(datos, 1_000)


def test_el_limite_duro_de_la_configuracion_se_aplica_de_verdad(fuente, atributos):
    """RNF-010: el limite es configurable y se respeta."""
    for numero in range(4, 200):
        fuente.anadir(numero, heading=f"Requisito {numero}", text="y" * 2_000)
    ajustes = Settings(module_path=MODULO, sync_attributes=atributos, hard_max_response_chars=5_000)
    servidor = crear_servidor(ajustes, ClienteFalso(fuente))

    crudo = asyncio.run(
        servidor.call_tool("list_requirements", {"limit": 100})
    ).content[0].text

    assert len(crudo) <= 5_000
    assert json.loads(crudo)["truncated"]["omitted"] > 0
