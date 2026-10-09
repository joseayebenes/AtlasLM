"""Generacion de DXL: regresiones conocidas y escapado (RNF-008, RF-041, RF-040).

Estas pruebas no necesitan DOORS: comprueban el texto que se le enviaria. Varias existen
porque el fallo correspondiente ya ocurrio de verdad en este proyecto (seccion 10 y
ADR-014), y la mas importante es la que fija que **el DXL generado no contiene ni una sola
secuencia de escape**.
"""


import pytest

from doors_kb.sources.doors import dxl

# Testigo fijo para las pruebas; en produccion lo genera el cliente en cada llamada.
T = "testigo01"

# ---------------------------------------------------------------------------------------
# Preambulo: la regresion del salto de linea (RNF-008)
# ---------------------------------------------------------------------------------------


def test_el_preambulo_termina_en_un_salto_de_linea_real():
    """El fallo original: se generaba la secuencia \\n literal y DXL no parseaba el script."""
    preambulo = dxl.build_preamble(0)

    assert preambulo == "pragma runLim, 0\n"
    assert preambulo.endswith("\n")
    assert "\\n" not in preambulo


def test_ningun_script_contiene_la_secuencia_barra_n_en_su_preambulo():
    """La misma regresion, vigilada sobre todos los scripts que se generan."""
    scripts = [
        dxl.script_list_attributes("/Proyecto/Reqs", T),
        dxl.script_fetch_page("/Proyecto/Reqs", ["Object Text"], T),
        dxl.script_get_requirement("/Proyecto/Reqs", 7, ["Object Text"], T),
        dxl.script_search("/Proyecto/Reqs", "timeout", ["Object Text"], T),
        dxl.script_get_links("/Proyecto/Reqs", 7, T),
    ]

    for script in scripts:
        primera_linea = script.split("\n", 1)[0]
        assert primera_linea == "pragma runLim, 0"
        assert "\\n" not in primera_linea


def test_el_watchdog_de_dxl_es_configurable():
    """RNF-007: 0 desactiva runLim y deja el control al timeout externo de Python."""
    assert dxl.build_preamble(0).startswith("pragma runLim, 0")
    assert dxl.build_preamble(500_000).startswith("pragma runLim, 500000")


# ---------------------------------------------------------------------------------------
# Escapado de valores (RF-041)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ('comillas "dobles"', 'comillas \\"dobles\\"'),
        ("barra\\invertida", "barra\\\\invertida"),
        ("dos\nlineas", "dos\\nlineas"),
        ("tabulado\tdentro", "tabulado\\tdentro"),
        ("normal", "normal"),
    ],
)
def test_el_escapado_neutraliza_los_caracteres_peligrosos(entrada, esperado):
    assert dxl.escape_dxl_string(entrada) == esperado


def test_los_caracteres_de_control_se_descartan():
    """DXL no sabe representarlos y romperian el literal; no aportan nada a una busqueda."""
    assert dxl.escape_dxl_string("texto\x00con\x07control") == "textoconcontrol"


def test_una_ruta_de_modulo_con_comillas_no_puede_cerrar_el_literal():
    """El vector de inyeccion mas directo: una ruta que cierra la cadena y anade codigo."""
    script = dxl.script_fetch_page('/Proyecto/"; delete m; //', ["Object Text"], T)

    # La comilla llega escapada, asi que sigue formando parte del literal de la ruta.
    assert '\\"; delete m; //' in script
    # Y no queda ninguna ocurrencia sin escapar que cerraria la cadena.
    assert '/Proyecto/";' not in script


def test_el_dxl_generado_no_contiene_ninguna_secuencia_de_escape():
    """El invariante que sostiene ADR-014 y que evita el fallo que tumbo la primera prueba real.

    Al construir JSON dentro de DXL habia tres lenguajes de escapado encadenados y bastaba
    equivocarse en uno para corromper la respuesta. Con el formato de campos con longitud no
    hace falta escapar nada en la salida, asi que un backslash en el script generado es
    senal de que alguien ha vuelto a introducir escapado.
    """
    scripts = [
        dxl.script_list_attributes("/Proyecto/Reqs", T),
        dxl.script_fetch_page("/Proyecto/Reqs", ["Object Heading", "Object Text"], T),
        dxl.script_get_requirement("/Proyecto/Reqs", 7, ["Object Text"], T),
        dxl.script_search("/Proyecto/Reqs", "timeout", ["Object Text"], T),
        dxl.script_get_links("/Proyecto/Reqs", 7, T),
    ]

    for script in scripts:
        assert "\\" not in script


def test_los_valores_viajan_con_su_longitud_delante():
    """Es lo que permite que un requisito con comillas o saltos de linea no necesite escapes."""
    script = dxl.script_list_attributes("/Proyecto/Reqs", T)

    assert "string ns(string s)" in script
    assert 'return cabecera ":" s' in script
    assert "b += ns(ad.name)" in script


def test_cada_respuesta_declara_la_marca_del_protocolo():
    """Distingue una respuesta valida de un mensaje suelto del interprete DXL."""
    for script in (
        dxl.script_list_attributes("/P/R", T),
        dxl.script_fetch_page("/P/R", ["Object Text"], T),
        dxl.script_search("/P/R", "x", ["Object Text"], T),
    ):
        assert f'ns("{dxl.PROTOCOLO}")' in script


def test_los_modulos_se_abren_siempre_en_lectura():
    """RF-004 y ADR-003: nunca se abre un modulo en modo edicion."""
    for script in (
        dxl.script_list_attributes("/Proyecto/Reqs", T),
        dxl.script_fetch_page("/Proyecto/Reqs", ["Object Text"], T),
        dxl.script_get_links("/Proyecto/Reqs", 1, T),
    ):
        assert 'read("/Proyecto/Reqs", false)' in script
        assert "edit(" not in script
        assert "share(" not in script


def test_un_modulo_que_no_abre_produce_un_error_explicito():
    """Seccion 10: NO_MODULE es un error, no una lista vacia de requisitos."""
    script = dxl.script_fetch_page("/Proyecto/Reqs", ["Object Text"], T)

    assert 'ns("ERROR")' in script
    assert 'ns("NO_MODULE")' in script
    assert "halt" in script


def test_la_paginacion_salta_al_cursor_en_lugar_de_recorrer_desde_el_principio():
    """ADR-006: es la diferencia entre coste lineal y los DXL Execution Timeout."""
    primera = dxl.script_fetch_page("/Proyecto/Reqs", ["Object Text"], T, cursor=None)
    siguiente = dxl.script_fetch_page("/Proyecto/Reqs", ["Object Text"], T, cursor=250)

    assert "Object o = first(m)" in primera
    assert "object(250, m)" in siguiente
    assert "next(cursorObj)" in siguiente


def test_los_borrados_y_las_filas_de_tabla_se_excluyen_por_defecto():
    """RF-023 y RF-024, con opcion explicita de inclusion."""
    por_defecto = dxl.script_fetch_page("/Proyecto/Reqs", ["Object Text"], T)
    con_todo = dxl.script_fetch_page(
        "/Proyecto/Reqs", ["Object Text"], T, include_deleted=True, include_table_internals=True
    )

    assert "isDeleted(o)" in por_defecto
    assert "table(o) || row(o) || cell(o)" in por_defecto
    assert "isDeleted(o)" not in con_todo.split("while (!null o)")[1]


def test_el_truncado_por_atributo_viaja_dentro_del_script():
    """RNF-011: se recorta en DOORS, no despues, para no traer texto que se va a tirar."""
    script = dxl.script_fetch_page("/Proyecto/Reqs", ["Object Text"], T, max_attribute_chars=500)

    assert 'ns(cut(o."Object Text" "", 500))' in script


def test_la_busqueda_distingue_mayusculas_solo_cuando_se_pide():
    """RF-032."""
    insensible = dxl.script_search("/Proyecto/Reqs", "tcp", ["Object Text"], T)
    sensible = dxl.script_search("/Proyecto/Reqs", "tcp", ["Object Text"], T, case_sensitive=True)

    assert "lower(valor)" in insensible
    assert "lower(valor)" not in sensible


def test_la_busqueda_por_expresion_regular_usa_el_motor_de_dxl():
    """RF-033."""
    script = dxl.script_search("/Proyecto/Reqs", r"\d+ segundos", ["Object Text"], T, regex=True)

    assert "regexp2(" in script


def test_la_trazabilidad_emite_el_sentido_de_cada_enlace():
    """RF-035: el cliente necesita saber si el enlace entra o sale para orientarlo bien.

    Que la respuesta no cubra enlaces OSLC (RF-037) lo declara el servidor MCP, no el DXL.
    """
    script = dxl.script_get_links("/Proyecto/Reqs", 7, T)

    assert 'ns("outgoing")' in script
    assert 'ns("incoming")' in script


def test_la_trazabilidad_entrante_reporta_los_modulos_que_no_pudo_cargar():
    """RF-036: un modulo origen sin permisos no puede pasar por 'sin enlaces'."""
    script = dxl.script_get_links("/Proyecto/Reqs", 7, T, direction="incoming")

    assert "fallos += ns(fullName(otro))" in script
    assert "nFallos" in script


def test_la_direccion_de_la_trazabilidad_filtra_los_bloques_generados():
    """RF-035."""
    salientes = dxl.script_get_links("/Proyecto/Reqs", 7, T, direction="outgoing")
    entrantes = dxl.script_get_links("/Proyecto/Reqs", 7, T, direction="incoming")

    assert 'o -> "*"' in salientes and 'o <- "*"' not in salientes
    assert 'o <- "*"' in entrantes and 'o -> "*"' not in entrantes


def test_los_atributos_se_emiten_en_el_orden_pedido():
    """Los nombres no viajan en la respuesta: el orden es el contrato con el cliente."""
    script = dxl.script_fetch_page("/Proyecto/Reqs", ["Object Heading", "Object Text"], T)

    posicion_titulo = script.index('o."Object Heading"')
    posicion_texto = script.index('o."Object Text"')
    assert posicion_titulo < posicion_texto


def test_el_recorte_reserva_sitio_para_su_propio_marcador():
    """RNF-011: un valor recortado tampoco puede superar el limite pactado."""
    script = dxl.script_fetch_page("/Proyecto/Reqs", ["Object Text"], T, max_attribute_chars=100)

    assert "int marca = 14" in script
    assert "s[0 : maxChars - marca - 1]" in script


def test_los_valores_de_enumeracion_solo_se_leen_en_enumeraciones():
    """El fallo que aborto la primera lectura real de atributos.

    at.size solo existe en los tipos de enumeracion. Consultarlo en un Integer, un String,
    un Date o un Text aborta el script con "wrong attribute type for Enumeration", que es
    justo lo que aparecio en la ventana 'DXL output' de DOORS.
    """
    script = dxl.script_list_attributes("/Proyecto/Reqs", "abc123")

    # Toda lectura de at.size (fuera de los comentarios) tiene que ir dentro de la guarda.
    lecturas = [
        linea.strip()
        for linea in script.split("\n")
        if "at.size" in linea and not linea.strip().startswith("//")
    ]
    assert lecturas == ["if (at.type == attrEnumeration) { n = at.size }"]


def test_cada_script_lleva_el_testigo_de_su_llamada():
    """Sin el, una respuesta rancia de DOORS pasaria por buena."""
    scripts = [
        dxl.script_list_attributes("/P/R", "token01"),
        dxl.script_fetch_page("/P/R", ["Object Text"], "token02"),
        dxl.script_get_requirement("/P/R", 1, ["Object Text"], "token03"),
        dxl.script_search("/P/R", "x", ["Object Text"], "token04"),
        dxl.script_get_links("/P/R", 1, "token05"),
    ]

    for numero, script in enumerate(scripts, start=1):
        assert f'ns("token0{numero}")' in script


def test_el_testigo_tambien_viaja_en_la_respuesta_de_error():
    """Un NO_MODULE de una llamada anterior tampoco puede confundirse con el de esta."""
    script = dxl.script_fetch_page("/P/R", ["Object Text"], "tokenXYZ")

    cabecera_error = script[script.index("if (null m)") : script.index("halt")]
    assert 'e += ns("tokenXYZ")' in cabecera_error


def test_un_testigo_no_alfanumerico_se_rechaza():
    """El testigo se inserta sin escapar en el script: tiene que ser inofensivo por diseno."""
    with pytest.raises(ValueError, match="alfanumerico"):
        dxl.script_list_attributes("/P/R", 'x"); halt; //')


def test_la_conversion_de_numero_pone_el_numero_delante():
    """Direccion confirmada contra DOORS real con doors-selftest.

    Las dos formas se probaron sobre la instalacion: `42 ""` funciona y `"" 42` no. Una
    version anterior de este proyecto asumio lo contrario, y el autodiagnostico fue lo que
    lo desmintio.
    """
    script = dxl.script_fetch_page("/P/R", ["Object Text"], "testigo01")

    assert 'return v ""' in script
    for linea in script.split("\n"):
        codigo = linea.strip()
        if codigo.startswith("//"):
            continue
        assert '"" ' not in codigo, f"conversion invertida en: {codigo}"


def test_la_longitud_se_convierte_usando_una_variable_intermedia():
    """`length(s) ""` se interpreta como `length(s "")` y devuelve un entero.

    Ese fue el "incorrect arguments for (=)" de la linea 4: la concatenacion se metia dentro
    de los parentesis de la llamada. Con una variable intermedia no hay ambiguedad.
    """
    script = dxl.script_fetch_page("/P/R", ["Object Text"], "testigo01")

    assert "int n = length(s)" in script
    codigo = [linea for linea in script.split("\n") if not linea.strip().startswith("//")]
    assert not any('length(s) ""' in linea for linea in codigo)
