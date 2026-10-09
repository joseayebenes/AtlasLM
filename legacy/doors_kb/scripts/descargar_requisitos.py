"""Descarga de todos los requisitos de DOORS Classic a la copia local en SQLite.

Segundo y ultimo archivo del script. Habla con DOORS por la interfaz Automation (COM/OLE),
ejecuta DXL de **solo lectura** y guarda lo que lee usando ``modelo_sqlite.py``.

    # un modulo concreto
    python scripts/descargar_requisitos.py --modulo "/Proyecto/Requisitos/SRS"

    # todos los modulos formales que cuelgan de una carpeta o proyecto
    python scripts/descargar_requisitos.py --carpeta "/Proyecto" --base doors.sqlite3

Necesita Windows, DOORS Classic instalado y ``pip install pywin32``. Python y DOORS deben
ejecutarse con el mismo usuario de Windows, porque Automation no cruza sesiones.

Las cuatro decisiones que gobiernan este archivo, todas nacidas de fallos reales contra
DOORS y ya validadas en el resto del proyecto:

1. **Sesion propia.** Se crea con ``Dispatch``, nunca con ``GetActiveObject``: una ventana
   de DOORS abierta a mano no aparece de forma fiable en la Running Object Table.
2. **Modulo explicito.** Cada script abre el modulo por su ``fullName`` con ``read(...)``.
   Dar por hecho que "el modulo ya esta abierto" produce errores NO_MODULE.
3. **Un solo hilo COM, con timeout externo.** El objeto Automation vive en el apartamento
   del hilo que lo creo, y un dialogo modal en DOORS colgaria el proceso para siempre.
4. **Ni una comilla en la respuesta.** Los scripts DXL no construyen JSON: emiten cada valor
   precedido de su longitud (``15:Absolute Number``) y es Python quien lo interpreta. Asi no
   hay tres capas de escapado encadenadas -Python, DXL y JSON- donde equivocarse.

El recorrido es por cursor: cada pagina continua en ``object(absno, m)`` y sigue con
``next(o)``, en lugar de recorrer el modulo desde el principio descartando objetos. Es la
diferencia entre coste lineal y coste cuadratico en modulos grandes.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import secrets
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modelo_sqlite import BaseLocal, Estadisticas, Requisito  # noqa: E402

logger = logging.getLogger("descarga-doors")

# Marca al principio de cada respuesta. Permite distinguir una respuesta del protocolo de un
# mensaje suelto del interprete DXL.
PROTOCOLO = "DKB1"


class ErrorDoors(RuntimeError):
    """Cualquier fallo al hablar con DOORS: sesion, modulo o ejecucion de DXL."""


# ---------------------------------------------------------------------------------------
# Generacion de DXL
# ---------------------------------------------------------------------------------------

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def escapar(valor: str) -> str:
    """Escapa un valor para insertarlo en un literal de cadena DXL.

    Es la unica puerta por la que un dato de Python entra en un script. Sin ella, una ruta de
    modulo con comillas cerraria el literal y el resto del texto se interpretaria como
    codigo. Los caracteres de control que DXL no sabe representar se descartan: no aportan
    nada y romperian el parseo.
    """
    salida = []
    for caracter in valor:
        if caracter in _ESCAPES:
            salida.append(_ESCAPES[caracter])
        elif ord(caracter) >= 0x20:
            salida.append(caracter)
    return "".join(salida)


# Emite un valor con su longitud delante. Ni comillas, ni llaves, ni escapes: un valor puede
# contener cualquier cosa -comillas, backslashes, saltos de linea- sin tratamiento especial,
# porque el lector de Python no busca delimitadores, cuenta caracteres.
_AUXILIARES = """
// El orden es `v ""`, con el numero delante; la forma inversa no compila.
string aTexto(int v) {
    return v ""
}

string ns(string s) {
    // La longitud se guarda en una variable antes de convertirla. Escrito del tiron,
    // `length(s) ""` se interpreta como `length(s "")`: la concatenacion se mete dentro de
    // los parentesis, la expresion devuelve un entero y la asignacion falla.
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
// El marcador importa: sin el, no se puede distinguir un texto corto de uno recortado.
string cut(string s, int maxChars) {
    if (maxChars <= 0) { return s }
    if (length(s) <= maxChars) { return s }
    int marca = 14
    if (maxChars <= marca) { return s[0 : maxChars - 1] }
    return s[0 : maxChars - marca - 1] "... [truncado]"
}
"""


def _preambulo(ciclos_limite: int = 0) -> str:
    """Preambulo con el watchdog interno de DXL.

    ``pragma runLim, 0`` lo desactiva y deja el control al timeout externo de Python, que es
    lo que interesa al descargar: el watchdog de DXL no distingue entre un script colgado y
    uno que simplemente recorre un modulo grande.

    Termina en un salto de linea **real**: generar la secuencia literal deja el pragma y la
    primera instruccion en la misma linea, y DXL falla con un error de sintaxis que no
    menciona el pragma.
    """
    return f"pragma runLim, {int(ciclos_limite)}\n"


def _cabecera(buffer: str, testigo: str, tipo: str) -> str:
    """Cabecera comun de toda respuesta: marca, testigo de la llamada y tipo.

    El testigo no es decorativo. Cuando un script DXL falla, ``oleSetResult`` no llega a
    ejecutarse y la propiedad ``result`` de DOORS conserva el valor de la llamada anterior.
    Sin testigo, Python leeria esa respuesta vieja creyendola nueva: una pagina se repetiria
    y la descarga daria por visitados objetos que nunca vio.
    """
    if not testigo.isalnum():
        raise ValueError(f"El testigo debe ser alfanumerico: {testigo!r}")
    return (
        f'{buffer} += ns("{PROTOCOLO}")\n'
        f'{buffer} += ns("{testigo}")\n'
        f'{buffer} += ns("{tipo}")\n'
    )


def _abrir_modulo(module_path: str, testigo: str) -> str:
    """Abre el modulo en lectura y aborta con un error explicito si no se puede."""
    return f"""
Module m = read("{escapar(module_path)}", false)
if (null m) {{
    Buffer e = create
{_cabecera("e", testigo, "ERROR")}    e += ns("NO_MODULE")
    oleSetResult(stringOf e)
    delete e
    halt
}}
"""


def script_modulos(carpeta: str, testigo: str, ciclos_limite: int = 0) -> str:
    """Lista recursivamente los modulos formales que cuelgan de una carpeta o proyecto.

    Es lo que convierte "descarga todos los requisitos" en una lista concreta de modulos. Los
    modulos de enlaces y los descriptivos se ignoran: no contienen requisitos.
    """
    return (
        _preambulo(ciclos_limite)
        + _AUXILIARES
        + f"""
Buffer datos = create
int total = 0

void listar(Folder f) {{
    if (null f) {{ return }}
    Item it
    for it in f do {{
        string t = type(it)
        if (t == "Formal") {{
            datos += ns(fullName(it))
            total++
        }} else if (t == "Folder" || t == "Project") {{
            listar(folder(it))
        }}
    }}
}}

Folder raiz = folder("{escapar(carpeta)}")
if (null raiz) {{
    Buffer e = create
{_cabecera("e", testigo, "ERROR")}    e += ns("NO_FOLDER")
    oleSetResult(stringOf e)
    delete e
    delete datos
    halt
}}
listar(raiz)

Buffer b = create
{_cabecera("b", testigo, "MODULES")}b += nsInt(total)
b += stringOf datos
oleSetResult(stringOf b)
delete datos
delete b
"""
    )


def script_atributos(module_path: str, testigo: str, ciclos_limite: int = 0) -> str:
    """Lista los atributos de objeto del modulo.

    Hay que preguntarlos antes de leer nada, por dos motivos: para poder descargar *todos*
    los atributos sin que el usuario los enumere, y porque leer un atributo inexistente con
    ``noError`` devuelve cadena vacia igual que uno vacio, asi que un nombre mal escrito
    pasaria desapercibido.
    """
    return (
        _preambulo(ciclos_limite)
        + _AUXILIARES
        + _abrir_modulo(module_path, testigo)
        + f"""
Buffer b = create
{_cabecera("b", testigo, "ATTRS")}
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
}}
oleSetResult(stringOf b)
delete b
"""
    )


def script_pagina(
    module_path: str,
    atributos: Sequence[str],
    testigo: str,
    *,
    cursor: int | None = None,
    tamano_pagina: int = 100,
    max_chars_atributo: int = 20_000,
    ciclos_limite: int = 0,
) -> str:
    """Lee una pagina de requisitos a partir del cursor.

    Emite ``PAGE``, el cursor siguiente (vacio si se llego al final del modulo) y, por cada
    requisito, su Absolute Number, identificador, outline number y los atributos pedidos en
    orden. Los nombres de atributo no viajan: el orden es el contrato con el lector.

    Que el cursor siguiente venga vacio es la unica senal de que el modulo se recorrio
    entero, y de ella depende que se puedan marcar ausentes como borrados. Por eso los
    requisitos se acumulan en un buffer aparte: el cursor no se conoce hasta terminar, y
    tiene que ir antes que los datos.
    """
    if cursor is None:
        posicionar = "Object o = first(m)\n"
    else:
        # object(absno, m) salta directamente al objeto por su Absolute Number. Si el objeto
        # del cursor ya no existe (lo han borrado entre dos paginas) se cae al recorrido
        # desde el principio: cuesta mas, pero no interrumpe la descarga.
        posicionar = f"""
Object o = null
Object cursorObj = object({int(cursor)}, m)
if (null cursorObj) {{
    o = first(m)
    while (!null o && (int)(o."Absolute Number") <= {int(cursor)}) {{ o = next(o) }}
}} else {{
    o = next(cursorObj)
}}
"""

    lectura = "".join(
        f'    datos += ns(cut(o."{escapar(nombre)}" "", {int(max_chars_atributo)}))\n'
        for nombre in atributos
    )
    return (
        _preambulo(ciclos_limite)
        + _AUXILIARES
        + _abrir_modulo(module_path, testigo)
        + posicionar
        + f"""
Buffer b = create
Buffer datos = create
int emitidos = 0
int ultimo = -1
bool agotado = true
while (!null o) {{
    if (emitidos >= {int(tamano_pagina)}) {{ agotado = false; break }}
    // Los objetos borrados y las celdas internas de las tablas no son requisitos.
    if (isDeleted(o) || table(o) || row(o) || cell(o)) {{ o = next(o); continue }}
    ultimo = (int)(o."Absolute Number")
    datos += nsInt(ultimo)
    datos += ns(identifier(o))
    datos += ns(number(o))
{lectura}    emitidos++
    o = next(o)
}}
{_cabecera("b", testigo, "PAGE")}if (agotado) {{ b += ns("") }} else {{ b += nsInt(ultimo) }}
b += nsInt(emitidos)
b += stringOf datos
oleSetResult(stringOf b)
delete datos
delete b
"""
    )


# ---------------------------------------------------------------------------------------
# Lectura de la respuesta
# ---------------------------------------------------------------------------------------


class Lector:
    """Recorre los campos de una respuesta en el orden en que los emitio el script."""

    def __init__(self, campos: list[str]) -> None:
        self._campos = campos
        self._posicion = 0

    def texto(self) -> str:
        if self._posicion >= len(self._campos):
            raise ErrorDoors(
                "La respuesta de DOORS se acabo antes de lo esperado: faltan campos. "
                "Suele indicar que llego cortada; prueba con --pagina mas pequena."
            )
        valor = self._campos[self._posicion]
        self._posicion += 1
        return valor

    def entero(self) -> int:
        crudo = self.texto()
        try:
            return int(crudo)
        except ValueError as exc:
            raise ErrorDoors(f"Se esperaba un numero de DOORS y llego {crudo[:80]!r}.") from exc

    def booleano(self) -> bool:
        return self.texto() == "1"

    def entero_opcional(self) -> int | None:
        """Campo numerico que puede venir vacio, como el cursor al final del modulo."""
        crudo = self.texto()
        return int(crudo) if crudo else None


def _trocear(crudo: str) -> list[str]:
    """Trocea la respuesta en campos comprobando que esta completa.

    Nunca devuelve un resultado parcial: una lista de requisitos incompleta que se tomara por
    completa provocaria borrados logicos indebidos aguas abajo.
    """
    if not crudo:
        raise ErrorDoors(
            "DOORS devolvio una respuesta vacia. Comprueba que la sesion Automation sigue "
            "viva y que el script DXL no fue interrumpido."
        )
    if ":" not in crudo[:40]:
        raise ErrorDoors(
            f"DOORS no devolvio una respuesta del protocolo, sino un mensaje del interprete "
            f"DXL ({len(crudo)} caracteres): {crudo[:2000]!r}"
        )

    campos: list[str] = []
    posicion = 0
    total = len(crudo)
    while posicion < total:
        separador = crudo.find(":", posicion)
        if separador < 0:
            raise ErrorDoors(
                f"Respuesta mal formada en la posicion {posicion}: se esperaba la longitud "
                f"de un campo. Fragmento: {crudo[posicion : posicion + 120]!r}"
            )
        try:
            longitud = int(crudo[posicion:separador])
        except ValueError as exc:
            # Se incluye la respuesta entera y no solo la cabecera: cuando el script falla,
            # lo que llega aqui es el mensaje del interprete DXL, y ese texto es lo unico
            # que permite localizar el error.
            raise ErrorDoors(
                f"Respuesta mal formada en la posicion {posicion}: "
                f"{crudo[posicion:separador][:80]!r} no es la longitud de un campo. "
                f"Respuesta completa ({len(crudo)} caracteres): {crudo[:2000]!r}"
            ) from exc
        inicio = separador + 1
        fin = inicio + longitud
        if fin > total:
            raise ErrorDoors(
                f"La respuesta de DOORS llego cortada: un campo declara {longitud} "
                f"caracteres y solo quedan {total - inicio}. Reduce --pagina o "
                "--max-chars-atributo."
            )
        campos.append(crudo[inicio:fin])
        posicion = fin
    return campos


def parsear(crudo: str, testigo: str) -> tuple[str, Lector]:
    """Convierte la respuesta en (tipo, lector) y comprueba que es la de **esta** llamada."""
    lector = Lector(_trocear(crudo))

    if lector.texto() != PROTOCOLO:
        raise ErrorDoors(
            f"La respuesta no empieza por la marca del protocolo ({PROTOCOLO}). Suele ser un "
            f"mensaje del interprete DXL: {crudo[:2000]!r}"
        )
    recibido = lector.texto()
    if recibido != testigo:
        raise ErrorDoors(
            "DOORS ha devuelto el resultado de una llamada anterior, no el de esta "
            f"(testigo esperado {testigo!r}, recibido {recibido!r}). Significa que el script "
            "DXL no llego a terminar: abre la ventana 'DXL output' en DOORS para ver el "
            "error del interprete."
        )
    return lector.texto(), lector


# ---------------------------------------------------------------------------------------
# Sesion con DOORS
# ---------------------------------------------------------------------------------------


class SesionDoors:
    """Sesion Automation de solo lectura, serializada en un unico hilo COM.

    Todas las llamadas se encolan hacia un hilo dedicado que hace ``CoInitialize`` una vez,
    y cada una tiene un timeout. Tras un timeout la sesion queda marcada como inutilizable:
    no se sabe si el script DXL sigue corriendo dentro de DOORS, y reutilizarla podria
    mezclar la respuesta de una llamada con la siguiente.
    """

    def __init__(
        self,
        *,
        prog_id: str = "DOORS.Application",
        timeout_arranque: int = 60,
        timeout_dxl: int = 300,
        ciclos_limite: int = 0,
    ) -> None:
        self.prog_id = prog_id
        self.timeout_arranque = timeout_arranque
        self.timeout_dxl = timeout_dxl
        self.ciclos_limite = ciclos_limite
        self._doors: Any = None
        self._cola: queue.Queue[tuple[Callable[[], Any], Future] | None] = queue.Queue()
        self._hilo: threading.Thread | None = None
        self._bloqueada = ""

    @property
    def bloqueada(self) -> bool:
        """La sesion quedo inutilizable tras un timeout y no admite mas llamadas."""
        return bool(self._bloqueada)

    # --- hilo COM ----------------------------------------------------------------------

    def _bucle(self) -> None:
        try:
            import pythoncom  # type: ignore[import-not-found]

            pythoncom.CoInitialize()
        except ImportError:  # pragma: no cover - solo fuera de Windows
            pythoncom = None
        try:
            while True:
                tarea = self._cola.get()
                if tarea is None:
                    return
                funcion, futuro = tarea
                if not futuro.set_running_or_notify_cancel():
                    continue
                try:
                    futuro.set_result(funcion())
                except BaseException as exc:  # se traslada intacta a quien espera
                    futuro.set_exception(exc)
        finally:
            if pythoncom is not None:  # pragma: no cover
                pythoncom.CoUninitialize()

    def _llamar(self, funcion: Callable[[], Any], *, timeout: float) -> Any:
        if self._bloqueada:
            raise ErrorDoors(
                f"La sesion con DOORS quedo bloqueada ({self._bloqueada}). Reinicia el "
                "proceso antes de volver a descargar."
            )
        if self._hilo is None or not self._hilo.is_alive():
            self._hilo = threading.Thread(target=self._bucle, name="doors-com", daemon=True)
            self._hilo.start()

        futuro: Future = Future()
        self._cola.put((funcion, futuro))
        try:
            return futuro.result(timeout=timeout)
        except FutureTimeoutError as exc:
            # No se cancela la llamada en curso porque no se puede: DXL sigue corriendo
            # dentro de DOORS. Lo unico honesto es dejar de usar esta sesion.
            self._bloqueada = f"timeout de {timeout} s"
            raise ErrorDoors(
                f"La llamada a DOORS supero el timeout de {timeout} segundos. El script DXL "
                "puede seguir ejecutandose dentro de DOORS, asi que la sesion queda "
                "inutilizable: reinicia el proceso. Si el modulo es grande, baja --pagina o "
                "sube --timeout-dxl."
            ) from exc

    # --- ciclo de vida -----------------------------------------------------------------

    def abrir(self) -> None:
        """Crea la sesion y espera a que responda a DXL.

        DOORS puede mostrar la ventana de login: la autenticacion la completa el usuario a
        mano, y por eso hay una espera con timeout en lugar de un fallo inmediato.
        """
        if self._doors is not None:
            return
        logger.info("Creando sesion Automation con ProgID '%s'", self.prog_id)
        self._doors = self._llamar(self._crear, timeout=self.timeout_arranque)

        limite = time.monotonic() + self.timeout_arranque
        avisado = False
        while time.monotonic() < limite:
            if self._responde():
                logger.info("Sesion de DOORS lista.")
                return
            if not avisado:
                logger.info(
                    "Esperando a que DOORS este disponible. Si ha aparecido la ventana de "
                    "login, completala en la ventana que ha abierto Python."
                )
                avisado = True
            time.sleep(1.0)
        raise ErrorDoors(
            f"La sesion de DOORS no estuvo lista en {self.timeout_arranque} segundos. "
            "Completa el inicio de sesion en la ventana que ha abierto Python (no en una "
            "anterior) y vuelve a intentarlo, o sube --timeout-arranque."
        )

    def _crear(self) -> Any:
        try:
            import win32com.client  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ErrorDoors(
                "pywin32 no esta instalado. El acceso a DOORS Classic solo funciona en "
                "Windows: instala pywin32 en la maquina donde este DOORS."
            ) from exc
        try:
            return win32com.client.Dispatch(self.prog_id)
        except Exception as exc:
            raise ErrorDoors(
                f"No se pudo crear la sesion Automation '{self.prog_id}': {exc}. Comprueba "
                "que DOORS Classic esta instalado y que Python y DOORS se ejecutan con el "
                "mismo usuario de Windows."
            ) from exc

    def cerrar(self) -> None:
        """Termina el hilo COM. No cierra DOORS: la sesion es de solo lectura."""
        if self._hilo is not None:
            self._cola.put(None)
            self._hilo.join(timeout=5.0)
            self._hilo = None
        self._doors = None

    def __enter__(self) -> SesionDoors:
        self.abrir()
        return self

    def __exit__(self, *_excepcion: object) -> None:
        self.cerrar()

    # --- ejecucion ---------------------------------------------------------------------

    @staticmethod
    def nuevo_testigo() -> str:
        return secrets.token_hex(8)

    def _responde(self) -> bool:
        """Sonda de disponibilidad con testigo unico.

        Con una respuesta fija daria por buena la sesion al leer el resultado de una llamada
        anterior, que es justo lo que pasa cuando un script falla y ``result`` se queda
        rancio.
        """
        testigo = self.nuevo_testigo()
        try:
            return self._dxl(f'oleSetResult("{testigo}")', timeout=self.timeout_arranque) == testigo
        except Exception:
            return False

    def _dxl(self, script: str, *, timeout: float | None = None) -> str:
        if self._doors is None:
            raise ErrorDoors("No hay sesion con DOORS: llama antes a abrir().")

        def ejecutar() -> str:
            self._doors.runStr(script)
            return str(self._doors.result or "")

        return self._llamar(ejecutar, timeout=timeout or self.timeout_dxl)

    def ejecutar(self, script: str, testigo: str, contexto: str) -> tuple[str, Lector]:
        """Ejecuta un script y devuelve su respuesta ya validada.

        Un ERROR declarado por el propio script se traduce a un mensaje con nombre propio;
        una respuesta que no sigue el protocolo casi siempre es un mensaje del interprete
        DXL, y se propaga con su texto, que es lo unico que permite diagnosticarlo.
        """
        tipo, lector = parsear(self._dxl(script), testigo)
        if tipo == "ERROR":
            codigo = lector.texto()
            if codigo == "NO_MODULE":
                raise ErrorDoors(
                    f"DOORS no pudo abrir el modulo '{contexto}'. Comprueba la ruta completa "
                    "y que el usuario tiene permiso de lectura."
                )
            if codigo == "NO_FOLDER":
                raise ErrorDoors(f"DOORS no pudo abrir la carpeta o proyecto '{contexto}'.")
            raise ErrorDoors(f"El script DXL reporto '{codigo}' sobre '{contexto}'.")
        return tipo, lector

    # --- consultas ---------------------------------------------------------------------

    def modulos_de(self, carpeta: str) -> list[str]:
        """Rutas completas de todos los modulos formales bajo una carpeta o proyecto."""
        testigo = self.nuevo_testigo()
        _, lector = self.ejecutar(
            script_modulos(carpeta, testigo, self.ciclos_limite), testigo, carpeta
        )
        return [lector.texto() for _ in range(lector.entero())]

    def atributos_de(self, module_path: str) -> list[tuple[str, str, bool]]:
        """Atributos de objeto del modulo, como (nombre, tipo, es_de_sistema)."""
        testigo = self.nuevo_testigo()
        _, lector = self.ejecutar(
            script_atributos(module_path, testigo, self.ciclos_limite), testigo, module_path
        )
        return [
            (lector.texto(), lector.texto(), lector.booleano()) for _ in range(lector.entero())
        ]

    def pagina(
        self,
        module_path: str,
        atributos: Sequence[str],
        *,
        cursor: int | None,
        tamano_pagina: int,
        max_chars_atributo: int,
    ) -> tuple[list[Requisito], int | None]:
        """Una pagina de requisitos y el cursor siguiente (None si se acabo el modulo)."""
        testigo = self.nuevo_testigo()
        _, lector = self.ejecutar(
            script_pagina(
                module_path,
                atributos,
                testigo,
                cursor=cursor,
                tamano_pagina=tamano_pagina,
                max_chars_atributo=max_chars_atributo,
                ciclos_limite=self.ciclos_limite,
            ),
            testigo,
            module_path,
        )
        siguiente = lector.entero_opcional()
        requisitos = []
        for _ in range(lector.entero()):
            absoluto = lector.entero()
            identificador = lector.texto()
            outline = lector.texto()
            valores = {nombre: lector.texto() for nombre in atributos}
            requisitos.append(
                Requisito(
                    module_path=module_path,
                    absolute_number=absoluto,
                    identifier=identificador,
                    outline_number=outline,
                    attributes=valores,
                )
            )
        return requisitos, siguiente


# ---------------------------------------------------------------------------------------
# Descarga
# ---------------------------------------------------------------------------------------


@dataclass
class Opciones:
    tamano_pagina: int = 100
    max_chars_atributo: int = 20_000
    atributos: tuple[str, ...] = ()
    incluir_sistema: bool = False


def descargar_modulo(
    sesion: SesionDoors, base: BaseLocal, module_path: str, opciones: Opciones
) -> Estadisticas:
    """Descarga un modulo entero a la copia local y devuelve sus estadisticas.

    El orden de los pasos no es casual:

    1. Resolver y validar los atributos **antes** de escribir nada: descubrir a mitad del
       recorrido que un nombre estaba mal escrito deja la copia local a medias.
    2. Recorrer el modulo por paginas, cada pagina en su propia transaccion.
    3. Marcar ausentes como borrados **solo si se llego al final del modulo**.
    4. Cerrar el historial siempre, tanto en exito como en fallo.
    """
    stats = Estadisticas(module_path=module_path)
    run_id = base.iniciar_run(module_path)
    try:
        atributos = _resolver_atributos(sesion, module_path, opciones)
        logger.info("%s: %d atributos por requisito", module_path, len(atributos))

        with base.transaccion():
            base.registrar_modulo(module_path)

        vistos: list[int] = []
        cursor: int | None = None
        while True:
            requisitos, cursor = sesion.pagina(
                module_path,
                atributos,
                cursor=cursor,
                tamano_pagina=opciones.tamano_pagina,
                max_chars_atributo=opciones.max_chars_atributo,
            )
            with base.transaccion():
                for requisito in requisitos:
                    vistos.append(requisito.absolute_number)
                    stats.registrar(base.guardar(requisito))
            stats.pages += 1
            logger.info(
                "%s: pagina %d, %d requisitos (%d en total)",
                module_path,
                stats.pages,
                len(requisitos),
                stats.seen,
            )
            if cursor is None:
                stats.completed_module = True
                break

        # El marcado de ausentes depende de haber visto el final del modulo, no de que no
        # haya habido errores: con un recorrido parcial se borrarian requisitos vivos.
        with base.transaccion():
            stats.deleted = base.marcar_ausentes(module_path, vistos)
            base.anotar_frescura(module_path, completo=True)
    except BaseException as exc:
        stats.error = f"{type(exc).__name__}: {exc}"
        base.cerrar_run(run_id, stats)
        raise
    base.cerrar_run(run_id, stats)
    return stats


def _resolver_atributos(
    sesion: SesionDoors, module_path: str, opciones: Opciones
) -> tuple[str, ...]:
    """Decide que atributos se leen de cada objeto.

    Por defecto, **todos** los atributos de objeto del modulo menos los de sistema (Created
    By, Created On, Last Modified By...), que abultan y rara vez interesan; ``Object Heading``
    y ``Object Text`` son de sistema pero se conservan siempre, porque son el requisito.
    Con ``--atributos`` se restringe a una lista, y entonces se valida contra el esquema real
    del modulo en lugar de leer a ciegas.
    """
    definiciones = sesion.atributos_de(module_path)
    disponibles = {nombre for nombre, _tipo, _sistema in definiciones}

    if opciones.atributos:
        desconocidos = [n for n in opciones.atributos if n not in disponibles]
        if desconocidos:
            raise ErrorDoors(
                f"El modulo '{module_path}' no tiene estos atributos: "
                f"{', '.join(repr(n) for n in desconocidos)}. Disponibles: "
                f"{', '.join(sorted(disponibles))}"
            )
        return tuple(opciones.atributos)

    imprescindibles = ("Object Heading", "Object Text")
    elegidos = [
        nombre
        for nombre, _tipo, sistema in definiciones
        if nombre in imprescindibles or opciones.incluir_sistema or not sistema
    ]
    if not elegidos:
        raise ErrorDoors(f"El modulo '{module_path}' no declara ningun atributo de objeto.")
    return tuple(elegidos)


def descargar(
    module_paths: Sequence[str], ruta_base: str, sesion: SesionDoors, opciones: Opciones
) -> list[Estadisticas]:
    """Descarga varios modulos, sin que el fallo de uno interrumpa a los demas.

    Un modulo sin permiso de lectura es lo normal en un proyecto grande, y no es motivo para
    tirar abajo una descarga de horas: se anota y se sigue. Lo ya guardado de los modulos
    anteriores es coherente, porque cada pagina se confirmo en su propia transaccion.
    """
    resultados = []
    with BaseLocal(ruta_base) as base:
        for indice, module_path in enumerate(module_paths, start=1):
            logger.info("[%d/%d] Descargando %s", indice, len(module_paths), module_path)
            try:
                stats = descargar_modulo(sesion, base, module_path, opciones)
            except ErrorDoors as exc:
                logger.error("%s: %s", module_path, exc)
                resultados.append(Estadisticas(module_path=module_path, error=str(exc)))
                if sesion.bloqueada:
                    # Tras un timeout la sesion no admite mas llamadas: seguir con los
                    # modulos restantes solo produciria el mismo error repetido.
                    logger.error("Sesion bloqueada: se abandonan los modulos restantes.")
                    break
                continue
            logger.info("%s", stats.resumen())
            resultados.append(stats)
    return resultados


# ---------------------------------------------------------------------------------------
# Linea de comandos
# ---------------------------------------------------------------------------------------


def _argumentos(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Descarga todos los requisitos de DOORS Classic a una base SQLite.",
        epilog="Los valores por defecto se pueden fijar con DOORS_MODULE_PATH, "
        "DOORS_DB_PATH y DOORS_PROG_ID.",
    )
    parser.add_argument(
        "--modulo",
        action="append",
        default=[],
        metavar="RUTA",
        help="ruta completa de un modulo formal; se puede repetir",
    )
    parser.add_argument(
        "--carpeta",
        action="append",
        default=[],
        metavar="RUTA",
        help="carpeta o proyecto del que descargar todos sus modulos, recursivamente",
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("DOORS_DB_PATH", "doors.sqlite3"),
        help="fichero SQLite de destino (por defecto: doors.sqlite3)",
    )
    parser.add_argument(
        "--atributos",
        default="",
        metavar="A,B,C",
        help="lista de atributos a descargar; por defecto, todos los del modulo",
    )
    parser.add_argument(
        "--incluir-sistema",
        action="store_true",
        help="incluir tambien los atributos de sistema (Created By, Last Modified On...)",
    )
    parser.add_argument("--pagina", type=int, default=100, help="requisitos por llamada DXL")
    parser.add_argument(
        "--max-chars-atributo",
        type=int,
        default=20_000,
        help="limite de caracteres por atributo; lo que pase se recorta con una marca",
    )
    parser.add_argument("--prog-id", default=os.environ.get("DOORS_PROG_ID", "DOORS.Application"))
    parser.add_argument("--timeout-arranque", type=int, default=60, metavar="SEGUNDOS")
    parser.add_argument("--timeout-dxl", type=int, default=300, metavar="SEGUNDOS")
    parser.add_argument(
        "--solo-listar",
        action="store_true",
        help="listar los modulos que se descargarian y salir, sin tocar la base",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="traza detallada")

    argumentos = parser.parse_args(argv)
    if not argumentos.modulo and not argumentos.carpeta:
        del_entorno = os.environ.get("DOORS_MODULE_PATH", "").strip()
        if del_entorno:
            argumentos.modulo = [del_entorno]
        else:
            parser.error("indica al menos un --modulo o una --carpeta")
    return argumentos


def main(argv: Sequence[str] | None = None) -> int:
    argumentos = _argumentos(argv)
    logging.basicConfig(
        level=logging.DEBUG if argumentos.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    opciones = Opciones(
        tamano_pagina=argumentos.pagina,
        max_chars_atributo=argumentos.max_chars_atributo,
        atributos=tuple(n.strip() for n in argumentos.atributos.split(",") if n.strip()),
        incluir_sistema=argumentos.incluir_sistema,
    )

    try:
        with SesionDoors(
            prog_id=argumentos.prog_id,
            timeout_arranque=argumentos.timeout_arranque,
            timeout_dxl=argumentos.timeout_dxl,
        ) as sesion:
            modulos = list(argumentos.modulo)
            for carpeta in argumentos.carpeta:
                encontrados = sesion.modulos_de(carpeta)
                logger.info("%s: %d modulos formales", carpeta, len(encontrados))
                modulos.extend(encontrados)

            # Sin duplicados y en orden estable: una carpeta y un modulo suelto pueden
            # referirse al mismo modulo, y descargarlo dos veces no aporta nada.
            modulos = list(dict.fromkeys(modulos))
            if not modulos:
                logger.error("No hay ningun modulo que descargar.")
                return 1

            if argumentos.solo_listar:
                for module_path in modulos:
                    print(module_path)
                return 0

            resultados = descargar(modulos, argumentos.base, sesion, opciones)
    except ErrorDoors as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Descarga interrumpida por el usuario.")
        return 130

    fallidos = [r for r in resultados if r.error]
    print()
    print(f"Base: {argumentos.base}")
    for stats in resultados:
        print(f"  {stats.resumen()}" if not stats.error else f"  {stats.module_path}: FALLO")
    if fallidos:
        print(f"\n{len(fallidos)} modulo(s) con errores:")
        for stats in fallidos:
            print(f"  {stats.module_path}: {stats.error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
