"""Busqueda lexical local con SQLite FTS5 (RF-070, hito H4).

Es la linea base que ADR-007 pide tener antes de los embeddings: resuelve identificadores,
codigos y terminos exactos con latencia local, y da un punto de comparacion para medir que
aporta realmente la busqueda semantica.

El punto delicado de este modulo es el **escapado de la consulta**. La sintaxis MATCH de
FTS5 tiene operadores propios (``AND``, ``OR``, ``NOT``, ``NEAR``, ``*``, ``-``, comillas):
una cadena escrita por una persona o por un agente, como ``timeout AND`` o ``fallo "grave``,
es un error de sintaxis que aborta la consulta. Es el equivalente lexical del escapado de
DXL (RF-041), y se resuelve igual: hay una unica puerta por la que pasa el texto del usuario.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..db.repository import SqliteRepository

# Columnas del indice que contienen texto buscable, en el orden de `schema.sql`.
COLUMNAS = ("identifier", "heading", "text", "attributes")

# Un termino es cualquier secuencia de caracteres no separadores. Se parte asi, y no por
# espacios, para que la puntuacion no acabe dentro del literal entrecomillado.
_TERMINOS = re.compile(r"[^\s]+")


@dataclass(frozen=True)
class ResultadoBusqueda:
    """Una coincidencia en el indice local.

    ``rank`` es la puntuacion bm25 de SQLite: **mas negativo es mas relevante**. Se conserva
    tal cual en lugar de normalizarla, para que quien componga rankings (la busqueda
    hibrida) parta del valor original.
    """

    module_path: str
    absolute_number: int
    identifier: str
    heading: str
    snippet: str
    rank: float

    def to_dict(self) -> dict[str, object]:
        return {
            "module_path": self.module_path,
            "absolute_number": self.absolute_number,
            "identifier": self.identifier,
            "heading": self.heading,
            "snippet": self.snippet,
            "rank": self.rank,
        }


def preparar_consulta(texto: str, *, modo: str = "literal") -> str:
    """Convierte el texto del usuario en una expresion MATCH valida.

    ``literal`` (por defecto) entrecomilla cada termino, de modo que cualquier cadena es una
    busqueda valida: los operadores de FTS5 se tratan como palabras y no pueden romper la
    consulta ni cambiar su significado.

    ``advanced`` deja pasar la sintaxis de FTS5 para quien la conozca y quiera usar ``OR``,
    ``NEAR`` o prefijos con ``*``. Es una eleccion explicita de quien llama, nunca el
    comportamiento por defecto.
    """
    if modo not in ("literal", "advanced"):
        raise ValueError(f"Modo de consulta desconocido: '{modo}'. Usa 'literal' o 'advanced'.")
    if modo == "advanced":
        return texto.strip()

    terminos = []
    for bruto in _TERMINOS.findall(texto):
        # Dentro de un literal FTS5 la comilla doble se escapa duplicandola.
        terminos.append('"' + bruto.replace('"', '""') + '"')
    return " ".join(terminos)


def buscar_lexical(
    repositorio: SqliteRepository,
    texto: str,
    *,
    module_path: str | None = None,
    limit: int = 25,
    modo: str = "literal",
    columna: str | None = None,
) -> list[ResultadoBusqueda]:
    """Busca en el indice local y devuelve los resultados ordenados por relevancia.

    ``columna`` restringe la busqueda a un campo del indice (RF-031); sin ella se busca en
    todos. ``module_path`` limita el ambito a un modulo, que es el filtro estructurado mas
    habitual (RF-077).
    """
    consulta = preparar_consulta(texto, modo=modo)
    if not consulta:
        return []
    if columna is not None:
        if columna not in COLUMNAS:
            raise ValueError(
                f"Columna de busqueda desconocida: '{columna}'. "
                f"Disponibles: {', '.join(COLUMNAS)}."
            )
        consulta = f"{columna}: {consulta}"

    sql = [
        "SELECT module_path, absolute_number, identifier, heading,",
        "       snippet(requirements_fts, -1, '[', ']', '...', 12) AS fragmento,",
        "       bm25(requirements_fts) AS rank",
        "  FROM requirements_fts",
        " WHERE requirements_fts MATCH ?",
    ]
    parametros: list[object] = [consulta]
    if module_path is not None:
        sql.append("   AND module_path = ?")
        parametros.append(module_path)
    sql.append(" ORDER BY rank LIMIT ?")  # bm25: mas negativo primero
    parametros.append(limit)

    filas = repositorio.conn.execute("\n".join(sql), parametros).fetchall()
    return [
        ResultadoBusqueda(
            module_path=f["module_path"],
            absolute_number=int(f["absolute_number"]),
            identifier=f["identifier"],
            heading=f["heading"],
            snippet=f["fragmento"],
            rank=float(f["rank"]),
        )
        for f in filas
    ]
