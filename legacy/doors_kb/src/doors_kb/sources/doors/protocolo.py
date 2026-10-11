"""Lectura del formato que emiten los scripts DXL.

Cada valor viaja precedido de su longitud (``15:Absolute Number``), de modo que el lector no
busca delimitadores: cuenta caracteres. Un requisito puede contener comillas, backslashes o
saltos de linea sin que nada haya que escapar en ninguno de los dos lados (ADR-014).

El otro motivo para este formato es el diagnostico. La respuesta de DOORS puede llegar
cortada, y con JSON eso se manifestaba como un error de sintaxis que no distinguia una
respuesta truncada de una mal construida. Aqui la longitud declarada no cuadra con lo que
hay, y el error puede decir exactamente que paso.

Cada respuesta lleva ademas un **testigo** propio de la llamada. Cuando un script DXL falla,
``oleSetResult`` no llega a ejecutarse y la propiedad ``result`` de DOORS conserva lo que
devolvio la llamada anterior: sin testigo, Python leeria esa respuesta vieja creyendola
nueva.
"""

from __future__ import annotations

from ...errors import DxlExecutionError
from .dxl import PROTOCOLO


class LectorCampos:
    """Recorre los campos de una respuesta, en el orden en que los emitio el script.

    El orden es el contrato entre ``dxl.py`` y el cliente: los nombres no viajan, solo los
    valores. A cambio de esa rigidez, la respuesta ocupa la mitad y no hay nada que escapar.
    """

    def __init__(self, campos: list[str]) -> None:
        self._campos = campos
        self._posicion = 0

    def __len__(self) -> int:
        return len(self._campos) - self._posicion

    def texto(self) -> str:
        """Siguiente campo como texto."""
        if self._posicion >= len(self._campos):
            raise DxlExecutionError(
                "La respuesta de DOORS se acabo antes de lo esperado: faltan campos. "
                "Suele indicar que la respuesta llego cortada."
            )
        valor = self._campos[self._posicion]
        self._posicion += 1
        return valor

    def entero(self) -> int:
        crudo = self.texto()
        try:
            return int(crudo)
        except ValueError as exc:
            raise DxlExecutionError(
                f"Se esperaba un numero de DOORS y llego {crudo[:80]!r}."
            ) from exc

    def booleano(self) -> bool:
        return self.texto() == "1"

    def opcional_entero(self) -> int | None:
        """Campo numerico que puede venir vacio, como el cursor al final del modulo."""
        crudo = self.texto()
        return int(crudo) if crudo else None


def parsear(
    crudo: str, token: str, *, script: str | None = None
) -> tuple[str, LectorCampos]:
    """Convierte la respuesta de DOORS en (tipo, lector de campos).

    El tipo dice que script la produjo (``ATTRS``, ``PAGE``, ``REQ``, ``SEARCH``, ``LINKS``)
    o si es un error declarado por el propio script (``ERROR``).

    Comprueba que la respuesta corresponde **a esta llamada** y no a la anterior.
    """
    campos = _leer_campos(crudo)
    lector = LectorCampos(campos)

    marca = lector.texto()
    if marca != PROTOCOLO:
        raise DxlExecutionError(
            f"La respuesta de DOORS no empieza por la marca del protocolo ({PROTOCOLO}). "
            f"Suele ser un mensaje del interprete DXL: {crudo[:2000]!r}",
            script=script,
        )

    recibido = lector.texto()
    if recibido != token:
        raise DxlExecutionError(
            "DOORS ha devuelto el resultado de una llamada anterior, no el de esta "
            f"(testigo esperado {token!r}, recibido {recibido!r}). Significa que el script "
            "DXL no llego a terminar: abre la ventana 'DXL output' en DOORS para ver el "
            "error del interprete.",
            script=script,
        )
    return lector.texto(), lector


def _leer_campos(crudo: str) -> list[str]:
    """Trocea la respuesta en campos, comprobando que esta completa.

    Nunca devuelve un resultado parcial: si la respuesta esta cortada lo dice, porque una
    lista de requisitos incompleta que se toma por completa provocaria borrados logicos
    indebidos aguas abajo (RF-061).
    """
    if not crudo:
        raise DxlExecutionError(
            "DOORS devolvio una respuesta vacia. Comprueba que la sesion Automation sigue "
            "viva y que el script DXL no fue interrumpido."
        )
    if ":" not in crudo[:40]:
        # Ni siquiera parece una cabecera de campo: es un mensaje del interprete DXL.
        raise DxlExecutionError(
            f"DOORS no devolvio una respuesta del protocolo, sino un mensaje del "
            f"interprete DXL ({len(crudo)} caracteres): {crudo[:2000]!r}"
        )

    campos: list[str] = []
    posicion = 0
    total = len(crudo)
    while posicion < total:
        separador = crudo.find(":", posicion)
        if separador < 0:
            raise DxlExecutionError(
                f"Respuesta de DOORS mal formada en la posicion {posicion}: se esperaba la "
                f"longitud de un campo y no hay separador. Fragmento: "
                f"{crudo[posicion : posicion + 120]!r}"
            )
        cabecera = crudo[posicion:separador]
        try:
            longitud = int(cabecera)
        except ValueError as exc:
            # Se incluye la respuesta entera, no solo la cabecera: cuando el script falla,
            # lo que llega aqui es el mensaje del interprete DXL, y ese texto es lo unico
            # que permite localizar el error. Recortarlo dejaria el fallo sin diagnostico.
            raise DxlExecutionError(
                f"Respuesta de DOORS mal formada en la posicion {posicion}: "
                f"{cabecera[:80]!r} no es la longitud de un campo. Respuesta completa "
                f"({len(crudo)} caracteres): {crudo[:2000]!r}"
            ) from exc

        inicio = separador + 1
        fin = inicio + longitud
        if fin > total:
            raise DxlExecutionError(
                f"La respuesta de DOORS llego cortada: un campo declara {longitud} "
                f"caracteres y solo quedan {total - inicio}. La respuesta completa ocupaba "
                f"al menos {fin} caracteres y se recibieron {total}. Reduce el tamano de "
                "pagina (--page-size) o el limite por atributo (--max-attribute-chars)."
            )
        campos.append(crudo[inicio:fin])
        posicion = fin

    return campos
