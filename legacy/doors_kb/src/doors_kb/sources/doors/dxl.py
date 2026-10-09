"""Generacion de scripts DXL.

Este modulo no habla con DOORS: solo construye texto. Esa separacion permite probar en
cualquier plataforma las cosas que ya fallaron en este proyecto (seccion 10 y ADR-014).

**Los scripts no construyen JSON.** Emiten cada valor precedido de su longitud
(``15:Absolute Number``) y es Python quien monta el JSON. El motivo esta en ADR-014: al
generar JSON dentro de DXL habia tres lenguajes de escapado encadenados -Python, DXL y
JSON- y bastaba equivocarse en uno para producir una respuesta corrupta. Con este formato
**no hay ni una secuencia de escape en el DXL generado**, asi que no puede haber errores de
escapado en la salida.

Sigue existiendo escapado de **entrada** (``escape_dxl_string``): los valores que Python
inserta en un script -una ruta de modulo, un termino de busqueda- tienen que ser literales
cerrados para que un agente no pueda inyectar codigo DXL (RF-041, ADR-003).

Recorrido por cursor (ADR-006): los scripts de paginacion localizan el objeto del cursor con
``object(absno, m)`` y siguen con ``next(o)``, en lugar de recorrer el modulo desde el
principio descartando objetos. Es la diferencia entre coste lineal y coste cuadratico, y la
causa de los *DXL Execution Timeout* que motivaron el cambio (seccion 7.1).
"""

from __future__ import annotations

from collections.abc import Sequence

# Marca al principio de cada respuesta. Permite distinguir una respuesta del protocolo de un
# mensaje de error del interprete DXL, que llega como texto suelto.
PROTOCOLO = "DKB1"

# Caracteres que hay que escapar dentro de un literal de cadena DXL.
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def escape_dxl_string(valor: str) -> str:
    """Escapa un valor para insertarlo en un literal de cadena DXL (RF-041).

    Es la unica puerta por la que un dato de Python entra en un script. Sin ella, una ruta
    de modulo o un termino de busqueda con comillas cerraria el literal y el resto del texto
    se interpretaria como codigo.

    Los caracteres de control que DXL no sabe representar se descartan en lugar de
    escaparse: no aportan nada a una busqueda y romperian el parseo.
    """
    salida = []
    for caracter in valor:
        if caracter in _ESCAPES:
            salida.append(_ESCAPES[caracter])
        elif ord(caracter) < 0x20:
            continue
        else:
            salida.append(caracter)
    return "".join(salida)


def build_preamble(run_limit_cycles: int = 0) -> str:
    """Genera el preambulo del script con el watchdog interno de DXL (RNF-007).

    ``pragma runLim, 0`` desactiva el limite interno y deja el control al timeout externo de
    Python (RNF-004), que es la configuracion recomendada para sincronizar: el watchdog de
    DXL no distingue entre un script colgado y uno que simplemente recorre un modulo grande.

    Termina en un salto de linea **real**. Es un detalle diminuto con una regresion propia:
    generar la secuencia ``\\n`` literal deja el pragma y la primera instruccion en la misma
    linea y DXL falla con un error de sintaxis que no menciona el pragma (RNF-008).
    """
    return f"pragma runLim, {int(run_limit_cycles)}\n"


# ---------------------------------------------------------------------------------------
# Funciones auxiliares que se inyectan en todos los scripts
# ---------------------------------------------------------------------------------------

# Emite un valor con su longitud delante. Ni comillas, ni llaves, ni escapes: un valor puede
# contener cualquier cosa -comillas, backslashes, saltos de linea- sin tratamiento especial,
# porque el lector de Python no busca delimitadores, cuenta caracteres.
_HELPERS = """
// Conversion de numero a texto. El orden es `v ""`, con el numero delante: confirmado
// contra DOORS real con doors-selftest, que probo las dos formas y descarto la inversa.
string aTexto(int v) {
    return v ""
}

string ns(string s) {
    // La longitud se guarda en una variable antes de convertirla. Escrito del tiron,
    // `length(s) ""` se interpreta como `length(s "")`: la concatenacion se mete dentro de
    // los parentesis de la llamada, la expresion devuelve un entero y la asignacion falla
    // con "incorrect arguments for (=)" (ADR-016).
    int n = length(s)
    string cabecera = aTexto(n)
    return cabecera ":" s
}

string nsInt(int v) {
    return ns(aTexto(v))
}

string nsBool(bool v) {
    if (v) { return ns("1") }
    return ns("0")
}

// Recorta dejando sitio para el marcador, de modo que el resultado nunca supere maxChars.
// El marcador importa: sin el, el agente no puede distinguir un texto corto de uno recortado
// y podria concluir que un requisito dice menos de lo que dice (RNF-011).
string cut(string s, int maxChars) {
    if (maxChars <= 0) { return s }
    if (length(s) <= maxChars) { return s }
    int marca = 14
    if (maxChars <= marca) { return s[0 : maxChars - 1] }
    return s[0 : maxChars - marca - 1] "... [truncado]"
}
"""


def _cabecera(buffer: str, token: str, tipo: str) -> str:
    """Cabecera comun de toda respuesta: marca, testigo de la llamada y tipo.

    El **testigo** es imprescindible, no decorativo. Cuando un script DXL falla,
    ``oleSetResult`` no llega a ejecutarse y la propiedad ``result`` de DOORS conserva el
    valor de la llamada anterior. Sin un testigo por llamada, Python leeria esa respuesta
    vieja creyendola nueva: una pagina de requisitos podria repetirse y el sincronizador
    daria por visitados objetos que nunca vio. Comprobar el testigo convierte ese riesgo
    silencioso en un error explicito.
    """
    if not token.isalnum():
        raise ValueError(f"El testigo de la llamada debe ser alfanumerico: {token!r}")
    return (
        f'{buffer} += ns("{PROTOCOLO}")\n'
        f'{buffer} += ns("{token}")\n'
        f'{buffer} += ns("{tipo}")\n'
    )


def _abrir_modulo(module_path: str, token: str) -> str:
    """Abre el modulo en lectura y aborta con un error explicito si no se puede (RF-004).

    Se abre siempre por ``fullName`` y de forma explicita: la sesion Automation que crea
    Python no comparte el modulo que el usuario tenga abierto en su ventana, y confiar en
    ello producia errores ``NO_MODULE`` (seccion 10, ADR-002).
    """
    ruta = escape_dxl_string(module_path)
    return f"""
Module m = read("{ruta}", false)
if (null m) {{
    Buffer e = create
{_cabecera("e", token, "ERROR")}    e += ns("NO_MODULE")
    oleSetResult(stringOf e)
    delete e
    halt
}}
"""


def _posicionar_cursor(cursor: int | None) -> str:
    """Situa el recorrido en el objeto siguiente al cursor (ADR-006, RF-058).

    ``object(absno, m)`` salta directamente al objeto por su Absolute Number, sin recorrer
    los anteriores. Si el objeto del cursor ya no existe (lo han borrado entre dos paginas),
    se cae al recorrido desde el principio: es correcto aunque cueste mas, y es preferible a
    interrumpir la sincronizacion.
    """
    if cursor is None:
        return "Object o = first(m)\n"
    return f"""
Object o = null
Object cursorObj = object({int(cursor)}, m)
if (null cursorObj) {{
    o = first(m)
    while (!null o && (int)(o."Absolute Number") <= {int(cursor)}) {{ o = next(o) }}
}} else {{
    o = next(cursorObj)
}}
"""


def _filtros_de_objeto(
    include_deleted: bool, include_table_internals: bool, respect_display_set: bool = False
) -> str:
    """Descarta los objetos que no son requisitos (RF-023, RF-024, RF-022).

    ``respect_display_set`` limita el recorrido a los objetos visibles en la vista actual
    del modulo (RF-022). Es una opcion **de consulta**: el sincronizador nunca la usa, y el
    motivo esta en ADR-012.

    Pendiente de validacion con DOORS real: el predicado de visibilidad se genera como
    ``isVisible(o)``; conviene confirmar el nombre exacto en la version instalada antes de
    dar por bueno el filtrado (ver docs/operacion_windows.md).
    """
    condiciones = []
    if not include_deleted:
        condiciones.append("isDeleted(o)")
    if not include_table_internals:
        condiciones.append("table(o) || row(o) || cell(o)")
    if respect_display_set:
        condiciones.append("!isVisible(o)")
    if not condiciones:
        return ""
    return "    if (" + " || ".join(f"({c})" for c in condiciones) + ") { o = next(o); continue }\n"


def _emitir_atributos(
    attributes: Sequence[str], max_attribute_chars: int, buffer: str = "b"
) -> str:
    """Genera el fragmento que emite los atributos pedidos de un objeto, en orden.

    Los nombres se insertan escapados y como literales cerrados: no hay forma de que un
    nombre de atributo se convierta en codigo DXL (RF-041). El lector de Python conoce el
    orden, asi que no hace falta emitir los nombres.
    """
    lineas = []
    for nombre in attributes:
        escapado = escape_dxl_string(nombre)
        lineas.append(
            f'    {buffer} += ns(cut(o."{escapado}" "", {int(max_attribute_chars)}))\n'
        )
    return "".join(lineas)


# ---------------------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------------------


def script_list_attributes(module_path: str, token: str, run_limit_cycles: int = 0) -> str:
    """Lista los atributos de objeto del modulo con sus metadatos (RF-010, RF-011).

    Formato emitido: ``ATTRS``, numero de atributos y, por cada uno, nombre, tipo, si es de
    sistema, si es multivaluado, cuantos valores de enumeracion tiene y esos valores.
    """
    return (
        build_preamble(run_limit_cycles)
        + _HELPERS
        + _abrir_modulo(module_path, token)
        + f"""
Buffer b = create
{_cabecera("b", token, "ATTRS")}
int total = 0
AttrDef adc
for adc in m do {{
    if (adc.object) {{ total++ }}
}}
b += nsInt(total)

AttrDef ad
for ad in m do {{
    if (!ad.object) {{ continue }}
    AttrType at = ad.type
    b += ns(ad.name)
    b += ns(at.name "")
    b += nsBool(ad.system)
    b += nsBool(ad.multi)
    // at.size solo existe en los tipos de enumeracion. Consultarlo en un Integer, un
    // String, un Date o un Text aborta el script con "wrong attribute type for
    // Enumeration", que es como fallo la primera lectura de atributos contra DOORS real.
    int n = 0
    if (at.type == attrEnumeration) {{ n = at.size }}
    b += nsInt(n)
    int i
    for (i = 0; i < n; i++) {{
        b += ns(at.strings[i])
    }}
}}
oleSetResult(stringOf b)
delete b
"""
    )


def script_fetch_page(
    module_path: str,
    attributes: Sequence[str],
    token: str,
    *,
    cursor: int | None = None,
    page_size: int = 25,
    max_attribute_chars: int = 20_000,
    include_deleted: bool = False,
    include_table_internals: bool = False,
    respect_display_set: bool = False,
    run_limit_cycles: int = 0,
) -> str:
    """Lee una pagina de requisitos a partir del cursor (RF-020, RF-057, RF-058).

    Emite ``PAGE``, el cursor siguiente (vacio si se llego al final) y, por cada requisito,
    su Absolute Number, identificador, outline number y los atributos pedidos en orden.

    Que el cursor siguiente venga vacio es la unica senal de que el modulo se ha recorrido
    entero, y de ella depende que el sincronizador pueda marcar ausentes como eliminados
    (RF-061). Por eso los requisitos se acumulan en un buffer aparte: el cursor no se conoce
    hasta terminar el recorrido, y tiene que ir antes que los datos.
    """
    filtros = _filtros_de_objeto(include_deleted, include_table_internals, respect_display_set)
    return (
        build_preamble(run_limit_cycles)
        + _HELPERS
        + _abrir_modulo(module_path, token)
        + _posicionar_cursor(cursor)
        + f"""
Buffer b = create
Buffer datos = create
int emitidos = 0
int ultimo = -1
bool agotado = true
while (!null o) {{
    if (emitidos >= {int(page_size)}) {{ agotado = false; break }}
{filtros}    ultimo = (int)(o."Absolute Number")
    datos += nsInt(ultimo)
    datos += ns(identifier(o))
    datos += ns(number(o))
{_emitir_atributos(attributes, max_attribute_chars, "datos")}    emitidos++
    o = next(o)
}}
{_cabecera("b", token, "PAGE")}if (agotado) {{ b += ns("") }} else {{ b += nsInt(ultimo) }}
b += nsInt(emitidos)
b += stringOf datos
oleSetResult(stringOf b)
delete datos
delete b
"""
    )


def script_get_requirement(
    module_path: str,
    absolute_number: int,
    attributes: Sequence[str],
    token: str,
    *,
    max_attribute_chars: int = 20_000,
    run_limit_cycles: int = 0,
) -> str:
    """Obtiene un objeto concreto por su Absolute Number (RF-021).

    Emite ``REQ`` y un indicador de si existe; solo si existe vienen despues sus datos.
    """
    return (
        build_preamble(run_limit_cycles)
        + _HELPERS
        + _abrir_modulo(module_path, token)
        + f"""
Buffer b = create
{_cabecera("b", token, "REQ")}Object o = object({int(absolute_number)}, m)
if (null o) {{
    b += nsBool(false)
    oleSetResult(stringOf b)
    delete b
    halt
}}
b += nsBool(true)
b += nsInt((int)(o."Absolute Number"))
b += ns(identifier(o))
b += ns(number(o))
b += nsBool(isDeleted(o))
{_emitir_atributos(attributes, max_attribute_chars)}oleSetResult(stringOf b)
delete b
"""
    )


def script_search(
    module_path: str,
    query: str,
    attributes: Sequence[str],
    token: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    cursor: int | None = None,
    page_size: int = 25,
    max_attribute_chars: int = 20_000,
    respect_display_set: bool = False,
    run_limit_cycles: int = 0,
) -> str:
    """Busca texto literal o expresion regular dentro del modulo (RF-030..RF-034).

    La busqueda ocurre **dentro de DOORS**: al agente solo llegan las coincidencias, no el
    modulo entero (RF-030). Cada coincidencia indica en que atributo se encontro y en que
    posicion (RF-034).
    """
    patron = escape_dxl_string(query)
    comparacion = (
        f'Regexp patron = regexp2("{patron}")' if regex else f'string aguja = "{patron}"'
    )
    if regex:
        deteccion = "        if (patron valor) { encontrado = true; posicion = start(patron) }"
    elif case_sensitive:
        deteccion = (
            "        int p = index(valor, aguja)\n"
            "        if (p >= 0) { encontrado = true; posicion = p }"
        )
    else:
        deteccion = (
            "        int p = index(lower(valor), lower(aguja))\n"
            "        if (p >= 0) { encontrado = true; posicion = p }"
        )

    nombres = ", ".join(f'"{escape_dxl_string(n)}"' for n in attributes)
    filtros = _filtros_de_objeto(False, False, respect_display_set)
    return (
        build_preamble(run_limit_cycles)
        + _HELPERS
        + _abrir_modulo(module_path, token)
        + _posicionar_cursor(cursor)
        + f"""
{comparacion}
string nombres[] = {{{nombres}}}
Buffer b = create
Buffer datos = create
int emitidos = 0
int ultimo = -1
bool agotado = true
while (!null o) {{
    if (emitidos >= {int(page_size)}) {{ agotado = false; break }}
{filtros}    ultimo = (int)(o."Absolute Number")
    int j
    for (j = 0; j < {len(attributes)}; j++) {{
        string valor = o.(nombres[j]) ""
        bool encontrado = false
        int posicion = -1
{deteccion}
        if (encontrado) {{
            datos += nsInt(ultimo)
            datos += ns(identifier(o))
            datos += ns(number(o))
            datos += ns(nombres[j])
            datos += nsInt(posicion)
            datos += ns(cut(valor, {int(max_attribute_chars)}))
            emitidos++
            break
        }}
    }}
    o = next(o)
}}
{_cabecera("b", token, "SEARCH")}if (agotado) {{ b += ns("") }} else {{ b += nsInt(ultimo) }}
b += nsInt(emitidos)
b += stringOf datos
oleSetResult(stringOf b)
delete datos
delete b
"""
    )


def script_get_links(
    module_path: str,
    absolute_number: int,
    token: str,
    *,
    direction: str = "both",
    run_limit_cycles: int = 0,
) -> str:
    """Obtiene la trazabilidad estandar de un objeto (RF-035, RF-036).

    Para los enlaces entrantes hay que cargar en lectura los modulos origen, cosa que puede
    fallar por permisos; el script lo reporta en lugar de callarselo (RF-036). No cubre
    enlaces externos OSLC (RF-037).
    """
    salientes = direction in ("outgoing", "both")
    entrantes = direction in ("incoming", "both")
    bloques = []
    if salientes:
        bloques.append(
            """
Link l
for l in o -> "*" do {
    Object destino = target(l)
    if (null destino) { continue }
    datos += ns("outgoing")
    datos += ns(fullName(module(destino)))
    datos += nsInt((int)(destino."Absolute Number"))
    datos += ns(l."LinkModuleName" "")
    enlaces++
}
"""
        )
    if entrantes:
        bloques.append(
            """
// Los enlaces entrantes exigen tener cargados los modulos origen; si alguno no se puede
// cargar se anota y se sigue, en lugar de devolver una trazabilidad incompleta en silencio.
ModName_ otro
for otro in o <- "*" do {
    if (null read(fullName(otro), false)) {
        fallos += ns(fullName(otro))
        nFallos++
    }
}
Link li
for li in o <- "*" do {
    Object origen = source(li)
    if (null origen) { continue }
    datos += ns("incoming")
    datos += ns(fullName(module(origen)))
    datos += nsInt((int)(origen."Absolute Number"))
    datos += ns(li."LinkModuleName" "")
    enlaces++
}
"""
        )

    return (
        build_preamble(run_limit_cycles)
        + _HELPERS
        + _abrir_modulo(module_path, token)
        + f"""
Buffer b = create
{_cabecera("b", token, "LINKS")}Object o = object({int(absolute_number)}, m)
if (null o) {{
    b += nsBool(false)
    oleSetResult(stringOf b)
    delete b
    halt
}}
b += nsBool(true)

Buffer datos = create
Buffer fallos = create
int enlaces = 0
int nFallos = 0
{"".join(bloques)}
b += nsInt(nFallos)
b += stringOf fallos
b += nsInt(enlaces)
b += stringOf datos
oleSetResult(stringOf b)
delete fallos
delete datos
delete b
"""
    )
