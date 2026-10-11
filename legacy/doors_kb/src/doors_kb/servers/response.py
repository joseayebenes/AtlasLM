"""Control del tamano de las respuestas MCP.

Cubre RF-043, RF-044, RNF-010 y RNF-011. Toda respuesta del servidor pasa por aqui.

El problema que resuelve: ``limit`` multiplicado por ``max_attribute_chars`` puede producir
respuestas de megabytes que saturan el contexto del agente. La solucion tiene tres escalones
y ninguno es silencioso: el agente debe poder distinguir "no hay mas resultados" de "hay
mas, pero no caben".
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import ResponseTooLargeError

# Objetivo normal de tamano (RNF-010). Por debajo de el no se recorta nada.
OBJETIVO_NORMAL_CHARS = 150_000


def serializar(datos: dict[str, Any], limite: int) -> str:
    """Serializa la respuesta a JSON respetando el limite duro (RF-043, RF-044).

    Escalones:

    1. Si cabe, se devuelve tal cual.
    2. Si no cabe y hay una lista de resultados, se van quitando elementos del final y se
       anota el recorte en la propia respuesta.
    3. Si ni siquiera cabe un unico elemento, se lanza ``ResponseTooLargeError`` diciendo
       que parametro bajar. Devolver una lista vacia seria peor: el agente la leeria como
       "no hay resultados" cuando lo que pasa es que uno solo ya no cabe.

    Nunca se devuelve una respuesta recortada sin decirlo.
    """
    texto = _volcar(datos)
    if len(texto) <= limite:
        return texto

    clave = _clave_de_lista(datos)
    if clave is not None:
        elementos = list(datos[clave])
        total = len(elementos)
        while len(elementos) > 1:
            elementos.pop()
            recortado = dict(datos)
            recortado[clave] = elementos
            recortado["truncated"] = {
                "reason": "response_size_limit",
                "returned": len(elementos),
                "omitted": total - len(elementos),
                "limit_chars": limite,
                "hint": "Reduce 'limit' o 'max_attribute_chars', o continua con 'cursor'.",
            }
            texto = _volcar(recortado)
            if len(texto) <= limite:
                return texto

    raise ResponseTooLargeError(len(_volcar(datos)), limite)


def _volcar(datos: dict[str, Any]) -> str:
    return json.dumps(datos, ensure_ascii=False, indent=2)


def _clave_de_lista(datos: dict[str, Any]) -> str | None:
    """Localiza la lista de resultados que se puede recortar, si la hay."""
    for clave in ("records", "hits", "links", "attributes", "requirements"):
        if isinstance(datos.get(clave), list) and datos[clave]:
            return clave
    return None
