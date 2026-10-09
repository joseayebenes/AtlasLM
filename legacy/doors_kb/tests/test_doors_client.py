"""Cliente de DOORS: lectura del protocolo y errores, sin Windows ni DOORS.

No se puede probar COM aqui, pero si todo lo que lo rodea: como se interpreta lo que
devuelve DXL, que pasa cuando la respuesta esta cortada o no sigue el protocolo, y que
errores ve el usuario. El worker COM se usa de verdad, con la inicializacion de COM
sustituida.
"""

import re

import pytest

from doors_kb.config import Settings
from doors_kb.errors import DoorsModuleError, DoorsSessionError, DxlExecutionError
from doors_kb.sources.doors.client import DoorsComClient
from doors_kb.sources.doors.com_worker import ComWorker
from doors_kb.sources.doors.dxl import PROTOCOLO


def ns(valor: object) -> str:
    """Codifica un campo como lo hace el DXL: longitud, dos puntos y contenido."""
    texto = str(valor)
    return f"{len(texto)}:{texto}"


# El testigo real lo genera el cliente en cada llamada, asi que las respuestas preparadas
# llevan un hueco que DoorsFalso rellena con el que venga en el script.
TESTIGO = "{TESTIGO}"


def respuesta(tipo: str, *campos: object) -> str:
    return ns(PROTOCOLO) + TESTIGO + ns(tipo) + "".join(ns(c) for c in campos)


# Tres atributos: nombre, tipo, es_sistema, multivaluado, n_enumerados, [enumerados...]
ATRIBUTOS = respuesta(
    "ATTRS",
    3,
    "Object Heading", "Text", "1", "0", 0,
    "Object Text", "Text", "1", "0", 0,
    "Estado", "Enumeration", "0", "0", 2, "Propuesto", "Aprobado",
)


class DoorsFalso:
    """Imita el objeto Automation: recibe un script en runStr y deja la respuesta en result."""

    def __init__(self, respuestas: dict[str, str]) -> None:
        self.respuestas = respuestas
        self.scripts: list[str] = []
        self.result = ""

    def runStr(self, script: str) -> None:  # noqa: N802 (nombre impuesto por la API de DOORS)
        self.scripts.append(script)
        testigo = re.search(r'ns\("([0-9a-f]{16})"\)', script)
        for marca, salida in self.respuestas.items():
            if marca in script:
                self.result = salida.replace(
                    TESTIGO, ns(testigo.group(1)) if testigo else ""
                )
                return
        self.result = ""


def _cliente(respuestas: dict[str, str]) -> tuple[DoorsComClient, DoorsFalso]:
    worker = ComWorker(inicializador=lambda: None, finalizador=lambda: None)
    cliente = DoorsComClient(Settings(module_path="/P/Reqs"), worker=worker)
    falso = DoorsFalso(respuestas)
    cliente._doors = falso  # se inyecta la sesion ya creada
    return cliente, falso


# Marcas para distinguir que script se esta ejecutando.
ES_ATRIBUTOS = 'ns("ATTRS")'
ES_PAGINA = 'ns("PAGE")'
ES_REQUISITO = 'ns("REQ")'
ES_BUSQUEDA = 'ns("SEARCH")'
ES_ENLACES = 'ns("LINKS")'


# ---------------------------------------------------------------------------------------
# Esquema
# ---------------------------------------------------------------------------------------


def test_los_atributos_se_parsean_con_sus_metadatos():
    """RF-011: tipo, ambito y valores de enumeracion llegan al agente."""
    cliente, _ = _cliente({ES_ATRIBUTOS: ATRIBUTOS})
    try:
        atributos = cliente.list_object_attributes("/P/Reqs")

        assert [a.name for a in atributos] == ["Object Heading", "Object Text", "Estado"]
        estado = atributos[2]
        assert estado.type_name == "Enumeration"
        assert estado.enum_values == ("Propuesto", "Aprobado")
        assert estado.is_system is False
        assert atributos[0].is_system is True
    finally:
        cliente.close()


def test_el_esquema_se_consulta_una_sola_vez_por_modulo():
    """Consultarlo en cada pagina multiplicaria las llamadas DXL sin aportar nada."""
    cliente, falso = _cliente({ES_ATRIBUTOS: ATRIBUTOS})
    try:
        cliente.list_object_attributes("/P/Reqs")
        cliente.list_object_attributes("/P/Reqs")

        assert len(falso.scripts) == 1
    finally:
        cliente.close()


# ---------------------------------------------------------------------------------------
# Lectura de requisitos
# ---------------------------------------------------------------------------------------


def test_una_pagina_promueve_heading_y_texto_a_campos_propios():
    """RF-014: son los atributos de contenido predeterminados."""
    pagina = respuesta(
        "PAGE", 12, 1,
        12, "REQ-12", "2.1", "Timeout", "Se cierra tras 30 s.", "Aprobado",
    )
    cliente, _ = _cliente({ES_ATRIBUTOS: ATRIBUTOS, ES_PAGINA: pagina})
    try:
        resultado = cliente.fetch_page("/P/Reqs", ["Object Heading", "Object Text", "Estado"])

        registro = resultado.records[0]
        assert registro.heading == "Timeout"
        assert registro.text == "Se cierra tras 30 s."
        assert registro.attributes == {"Estado": "Aprobado"}
        assert registro.module_path == "/P/Reqs"
        assert resultado.next_cursor == 12
        assert resultado.exhausted is False
    finally:
        cliente.close()


def test_un_cursor_vacio_indica_el_final_del_modulo():
    """RF-061: de esta senal depende que se puedan marcar ausentes como eliminados."""
    cliente, _ = _cliente({ES_ATRIBUTOS: ATRIBUTOS, ES_PAGINA: respuesta("PAGE", "", 0)})
    try:
        assert cliente.fetch_page("/P/Reqs", ["Object Text"]).exhausted is True
    finally:
        cliente.close()


def test_un_valor_con_comillas_y_saltos_de_linea_llega_intacto():
    """El fallo que motivo ADR-014: con JSON hecho a mano en DXL, esto rompia el parseo."""
    texto = 'El sistema debera responder "OK"\nen menos de 30 s.\tSiempre. C:\\ruta'
    pagina = respuesta("PAGE", "", 1, 7, "REQ-7", "1", "Titulo", texto, "Aprobado")
    cliente, _ = _cliente({ES_ATRIBUTOS: ATRIBUTOS, ES_PAGINA: pagina})
    try:
        registro = cliente.fetch_page(
            "/P/Reqs", ["Object Heading", "Object Text", "Estado"]
        ).records[0]

        assert registro.text == texto
    finally:
        cliente.close()


def test_un_objeto_borrado_se_devuelve_como_ausente():
    """RF-023: los objetos borrados no son requisitos vigentes."""
    # existe=1, numero, identificador, outline, borrado=1, y despues sus atributos
    req = respuesta("REQ", "1", 3, "REQ-3", "1", "1", "Titulo", "Cuerpo", "Aprobado")
    cliente, _ = _cliente({ES_ATRIBUTOS: ATRIBUTOS, ES_REQUISITO: req})
    try:
        assert cliente.get_requirement(
            "/P/Reqs", 3, ["Object Heading", "Object Text", "Estado"]
        ) is None
    finally:
        cliente.close()


def test_un_objeto_inexistente_se_devuelve_como_ausente():
    cliente, _ = _cliente({ES_ATRIBUTOS: ATRIBUTOS, ES_REQUISITO: respuesta("REQ", "0")})
    try:
        assert cliente.get_requirement("/P/Reqs", 99, ["Object Text"]) is None
    finally:
        cliente.close()


def test_la_busqueda_indica_atributo_y_posicion():
    """RF-034."""
    busqueda = respuesta(
        "SEARCH", "", 1, 5, "REQ-5", "1.1", "Object Text", 22, "tras 30 segundos"
    )
    cliente, _ = _cliente({ES_ATRIBUTOS: ATRIBUTOS, ES_BUSQUEDA: busqueda})
    try:
        pagina = cliente.search("/P/Reqs", "30 segundos", ["Object Text"])

        hit = pagina.hits[0]
        assert hit.record.absolute_number == 5
        assert hit.matched_attribute == "Object Text"
        assert hit.match_start == 22
        assert pagina.exhausted is True
    finally:
        cliente.close()


def test_la_trazabilidad_orienta_bien_cada_enlace():
    """RF-035: en un entrante, el modulo consultado es el destino, no el origen."""
    enlaces = respuesta(
        "LINKS", "1", 0, 2,
        "outgoing", "/Otro/Destino", 44, "satisfies",
        "incoming", "/Otro/Origen", 7, "satisfies",
    )
    cliente, _ = _cliente({ES_ENLACES: enlaces})
    try:
        saliente, entrante = cliente.get_links("/P/Reqs", 12)

        assert (saliente.source_module, saliente.target_module) == ("/P/Reqs", "/Otro/Destino")
        assert saliente.target_absolute_number == 44
        assert (entrante.source_module, entrante.target_module) == ("/Otro/Origen", "/P/Reqs")
        assert entrante.source_absolute_number == 7
    finally:
        cliente.close()


def test_los_modulos_origen_que_no_cargan_se_reportan(caplog):
    """RF-036: un modulo sin permisos no puede pasar por 'sin enlaces'."""
    enlaces = respuesta("LINKS", "1", 1, "/Modulo/Sin/Permisos", 0)
    cliente, _ = _cliente({ES_ENLACES: enlaces})
    try:
        with caplog.at_level("WARNING"):
            assert cliente.get_links("/P/Reqs", 12) == []

        assert "/Modulo/Sin/Permisos" in caplog.text
    finally:
        cliente.close()


# ---------------------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------------------


def test_un_modulo_que_no_abre_produce_un_error_accionable():
    """Seccion 10: NO_MODULE es un error explicito, no una lista vacia."""
    cliente, _ = _cliente(
        {ES_ATRIBUTOS: ATRIBUTOS, ES_PAGINA: respuesta("ERROR", "NO_MODULE")}
    )
    try:
        with pytest.raises(DoorsModuleError, match="fullName"):
            cliente.fetch_page("/P/Reqs", ["Object Text"])
    finally:
        cliente.close()


def test_un_mensaje_del_interprete_dxl_conserva_su_texto():
    """Sin el texto original no hay forma de diagnosticar un fallo de DXL."""
    cliente, _ = _cliente({ES_ATRIBUTOS: "-E- DXL: <Line:12> incorrect arguments"})
    try:
        with pytest.raises(DxlExecutionError, match="incorrect arguments"):
            cliente.list_object_attributes("/P/Reqs")
    finally:
        cliente.close()


def test_una_respuesta_cortada_se_reconoce_como_tal():
    """El motivo principal de llevar la longitud delante: distinguir truncado de corrupto."""
    completa = respuesta("PAGE", "", 1, 7, "REQ-7", "1", "Titulo", "x" * 400, "Aprobado")
    cliente, _ = _cliente({ES_ATRIBUTOS: ATRIBUTOS, ES_PAGINA: completa[:200]})
    try:
        with pytest.raises(DxlExecutionError, match="llego cortada"):
            cliente.fetch_page("/P/Reqs", ["Object Heading", "Object Text", "Estado"])
    finally:
        cliente.close()


def test_una_respuesta_vacia_se_explica():
    cliente, _ = _cliente({})
    try:
        with pytest.raises(DxlExecutionError, match="respuesta vacia"):
            cliente.list_object_attributes("/P/Reqs")
    finally:
        cliente.close()


def test_un_atributo_inexistente_se_rechaza_antes_de_leer_la_pagina():
    """RF-012: la validacion va contra el esquema, no contra el valor leido."""
    cliente, falso = _cliente({ES_ATRIBUTOS: ATRIBUTOS})
    try:
        with pytest.raises(Exception, match="Atributos inexistentes"):
            cliente.fetch_page("/P/Reqs", ["Object Text", "Estdo"])

        # Solo se ejecuto el script del esquema: no se llego a pedir la pagina.
        assert len(falso.scripts) == 1
    finally:
        cliente.close()


def test_consultar_sin_sesion_dice_que_hacer():
    """RF-003: el primer paso siempre es abrir la sesion y autenticarse."""
    worker = ComWorker(inicializador=lambda: None, finalizador=lambda: None)
    cliente = DoorsComClient(Settings(module_path="/P/Reqs"), worker=worker)
    try:
        with pytest.raises(DoorsSessionError, match="start_doors_session"):
            cliente.list_object_attributes("/P/Reqs")

        assert cliente.status()["session"] == "not_started"
    finally:
        cliente.close()


def test_sin_pywin32_el_error_explica_que_falta():
    """El proyecto se desarrolla fuera de Windows: el mensaje tiene que ser claro."""
    import importlib.util

    if importlib.util.find_spec("win32com") is not None:
        pytest.skip("pywin32 disponible: este caso solo aplica fuera de Windows")

    worker = ComWorker(inicializador=lambda: None, finalizador=lambda: None)
    cliente = DoorsComClient(Settings(module_path="/P/Reqs"), worker=worker)
    try:
        with pytest.raises(DoorsSessionError, match="pywin32"):
            cliente.start_session(timeout=1)
    finally:
        cliente.close()


def test_una_respuesta_de_la_llamada_anterior_se_rechaza():
    """El segundo fallo que destapo la primera lectura real de atributos.

    Cuando un script DXL falla, oleSetResult no llega a ejecutarse y la propiedad result de
    DOORS conserva lo que devolvio la llamada anterior. Sin testigo, Python leeria esa
    respuesta vieja creyendola nueva: una pagina de requisitos podria repetirse y el
    sincronizador daria por visitados objetos que nunca vio.
    """
    # Respuesta bien formada pero con el testigo de otra llamada.
    vieja = ns(PROTOCOLO) + ns("0123456789abcdef") + ns("ATTRS") + ns(0)
    cliente, _ = _cliente({ES_ATRIBUTOS: vieja})
    try:
        with pytest.raises(DxlExecutionError, match="llamada anterior"):
            cliente.list_object_attributes("/P/Reqs")
    finally:
        cliente.close()


def test_el_error_de_testigo_apunta_a_la_ventana_de_dxl():
    """Es donde esta el mensaje del interprete, que es lo unico que explica el fallo."""
    vieja = ns(PROTOCOLO) + ns("0123456789abcdef") + ns("ATTRS") + ns(0)
    cliente, _ = _cliente({ES_ATRIBUTOS: vieja})
    try:
        with pytest.raises(DxlExecutionError, match="DXL output"):
            cliente.list_object_attributes("/P/Reqs")
    finally:
        cliente.close()


def test_cada_llamada_usa_un_testigo_distinto():
    """Un testigo fijo no distinguiria la respuesta de esta llamada de la de la anterior."""
    cliente, falso = _cliente({ES_ATRIBUTOS: ATRIBUTOS})
    try:
        cliente.list_object_attributes("/P/Reqs")
        cliente._esquema.clear()  # fuerza una segunda consulta real
        cliente.list_object_attributes("/P/Reqs")

        testigos = [re.search(r'ns\("([0-9a-f]{16})"\)', s).group(1) for s in falso.scripts]
        assert len(testigos) == 2
        assert testigos[0] != testigos[1]
    finally:
        cliente.close()
