"""Busqueda hibrida: fusion de los rankings lexical y vectorial (RF-076, hito H6).

Los dos modos fallan en sitios distintos, y ahi esta el motivo de combinarlos: FTS acierta
con identificadores, codigos y terminos exactos, pero no encuentra un requisito descrito con
otras palabras; la busqueda semantica encuentra por significado, pero puede pasar por alto
un identificador literal.

**Fusion por RRF** (Reciprocal Rank Fusion), que es lo que nombra el hito H6. Se combinan
las *posiciones* en cada ranking, no las puntuaciones: bm25 devuelve valores negativos sin
escala fija y la similitud coseno va de -1 a 1, de modo que sumarlas o promediarlas
requeriria normalizaciones arbitrarias que cambian con cada corpus. Las posiciones son
comparables sin calibrar nada, que es justo lo que hace a RRF robusto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db.repository import SqliteRepository
from ..embeddings.provider import EmbeddingProvider
from .lexical import buscar_lexical
from .vector import buscar_vectorial

# Constante clasica de RRF. Amortigua el peso de las primeras posiciones: sin ella, el
# primer resultado de un ranking dominaria cualquier combinacion.
K_RRF = 60


@dataclass
class ResultadoHibrido:
    """Un requisito encontrado por una de las dos vias, o por las dos.

    ``procedencia`` permite explicar por que aparece un resultado, que es lo que hace
    revisable una busqueda hibrida en lugar de una caja negra.
    """

    module_path: str
    absolute_number: int
    identifier: str
    heading: str
    score: float = 0.0
    procedencia: list[str] = field(default_factory=list)
    posicion_lexical: int | None = None
    posicion_vectorial: int | None = None
    snippet: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "module_path": self.module_path,
            "absolute_number": self.absolute_number,
            "identifier": self.identifier,
            "heading": self.heading,
            "score": round(self.score, 6),
            "matched_by": list(self.procedencia),
            "lexical_rank": self.posicion_lexical,
            "semantic_rank": self.posicion_vectorial,
            "snippet": self.snippet,
        }


def buscar_hibrida(
    repositorio: SqliteRepository,
    consulta: str,
    provider: EmbeddingProvider,
    *,
    module_path: str | None = None,
    filtros_atributos: dict[str, str] | None = None,
    limit: int = 25,
    peso_lexical: float = 1.0,
    peso_vectorial: float = 1.0,
    modo_lexical: str = "literal",
) -> list[ResultadoHibrido]:
    """Combina la busqueda lexical y la semantica en un unico ranking.

    Cada via se consulta con un margen sobre ``limit``: un requisito que sale decimo en las
    dos listas puede merecer estar entre los tres primeros del ranking combinado, y con
    listas cortadas justo en ``limit`` nunca llegaria a considerarse.

    Los pesos permiten inclinar el resultado hacia lo lexico o lo semantico sin cambiar el
    algoritmo. Con ``peso_vectorial=0`` se obtiene el comportamiento de FTS puro, lo que da
    una linea base con la que comparar (ADR-007).
    """
    margen = max(limit * 3, 30)

    lexicales = (
        buscar_lexical(
            repositorio, consulta, module_path=module_path, limit=margen, modo=modo_lexical
        )
        if peso_lexical > 0
        else []
    )
    vectoriales = (
        buscar_vectorial(
            repositorio,
            consulta,
            provider,
            module_path=module_path,
            filtros_atributos=filtros_atributos,
            limit=margen,
        )
        if peso_vectorial > 0
        else []
    )

    combinados: dict[tuple[str, int], ResultadoHibrido] = {}

    for posicion, resultado in enumerate(lexicales, start=1):
        clave = (resultado.module_path, resultado.absolute_number)
        entrada = combinados.setdefault(
            clave,
            ResultadoHibrido(
                module_path=resultado.module_path,
                absolute_number=resultado.absolute_number,
                identifier=resultado.identifier,
                heading=resultado.heading,
            ),
        )
        entrada.score += peso_lexical / (K_RRF + posicion)
        entrada.procedencia.append("lexical")
        entrada.posicion_lexical = posicion
        entrada.snippet = resultado.snippet

    for posicion, resultado in enumerate(vectoriales, start=1):
        clave = (resultado.module_path, resultado.absolute_number)
        entrada = combinados.setdefault(
            clave,
            ResultadoHibrido(
                module_path=resultado.module_path,
                absolute_number=resultado.absolute_number,
                identifier=resultado.identifier,
                heading=resultado.heading,
            ),
        )
        entrada.score += peso_vectorial / (K_RRF + posicion)
        entrada.procedencia.append("semantic")
        entrada.posicion_vectorial = posicion

    ordenados = sorted(
        combinados.values(),
        # A igualdad de puntuacion, el Absolute Number desempata: sin un criterio estable,
        # dos ejecuciones identicas podrian devolver ordenes distintos.
        key=lambda r: (-r.score, r.module_path, r.absolute_number),
    )
    return ordenados[:limit]
