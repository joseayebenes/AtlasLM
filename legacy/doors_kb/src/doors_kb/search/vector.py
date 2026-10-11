"""Busqueda semantica por similitud de embeddings (RF-075, RF-077, hito H6).

Estrategia: **fuerza bruta con numpy** (ADR-011). Sin extension nativa disponible
(`sqlite-vec` no lo esta) y con modulos de cientos a decenas de miles de objetos, un
producto escalar sobre una matriz resuelve en milisegundos. Se revisara si algun modulo se
acerca al orden de 10^5 requisitos.

Los vectores estan normalizados por el proveedor, asi que la similitud coseno es
directamente el producto escalar; aun asi se normaliza aqui para no depender de que un
proveedor futuro lo haga.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db.repository import SqliteRepository
from ..embeddings.provider import EmbeddingProvider


@dataclass(frozen=True)
class ResultadoVectorial:
    """Una coincidencia semantica. ``score`` es la similitud coseno: mas alto, mas parecido."""

    module_path: str
    absolute_number: int
    identifier: str
    heading: str
    score: float

    def to_dict(self) -> dict[str, object]:
        return {
            "module_path": self.module_path,
            "absolute_number": self.absolute_number,
            "identifier": self.identifier,
            "heading": self.heading,
            "score": self.score,
        }


def _importar_numpy():
    """Importa numpy con un error accionable si falta.

    Falla explicitamente en lugar de caer a una implementacion lenta en Python puro: una
    busqueda que tarda segundos sin decir por que es peor que uno que dice como arreglarlo.
    """
    try:
        import numpy  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depende del entorno
        from ..errors import DoorsKbError

        raise DoorsKbError(
            "La busqueda semantica necesita numpy. Instala el extra correspondiente:  "
            'pip install -e ".[embeddings]"'
        ) from exc
    return numpy


def buscar_vectorial(
    repositorio: SqliteRepository,
    consulta: str,
    provider: EmbeddingProvider,
    *,
    module_path: str | None = None,
    filtros_atributos: dict[str, str] | None = None,
    limit: int = 25,
) -> list[ResultadoVectorial]:
    """Devuelve los ``limit`` requisitos semanticamente mas proximos a la consulta.

    Los filtros estructurados se aplican al cargar los candidatos, **antes** de puntuar
    (RF-077). Filtrar despues de calcular un top-k devolveria menos resultados de los
    pedidos, o ninguno, aunque hubiera coincidencias validas mas abajo en el ranking.
    """
    if not consulta.strip():
        return []

    candidatos = repositorio.cargar_embeddings(
        provider.model, module_path=module_path, filtros_atributos=filtros_atributos
    )
    if not candidatos:
        return []

    numpy = _importar_numpy()
    matriz = numpy.array([c["vector"] for c in candidatos], dtype=numpy.float32)
    vector_consulta = numpy.array(provider.embed([consulta])[0], dtype=numpy.float32)

    if matriz.shape[1] != vector_consulta.shape[0]:
        from ..errors import DoorsKbError

        raise DoorsKbError(
            f"Los embeddings guardados tienen {matriz.shape[1]} dimensiones y el modelo "
            f"'{provider.model}' genera {vector_consulta.shape[0]}. El indice se genero con "
            "otro modelo: reconstruyelo con doors-embed."
        )

    matriz = _normalizar(numpy, matriz)
    vector_consulta = vector_consulta / (numpy.linalg.norm(vector_consulta) or 1.0)
    puntuaciones = matriz @ vector_consulta

    # argsort sobre el negativo da el orden descendente; se corta a limit antes de ordenar
    # del todo para no pagar un orden completo cuando solo hacen falta unos pocos.
    mejores = numpy.argsort(-puntuaciones)[:limit]
    return [
        ResultadoVectorial(
            module_path=candidatos[i]["module_path"],
            absolute_number=candidatos[i]["absolute_number"],
            identifier=candidatos[i]["identifier"],
            heading=candidatos[i]["heading"],
            score=float(puntuaciones[i]),
        )
        for i in mejores
    ]


def _normalizar(numpy, matriz):
    """Divide cada fila por su norma, dejando en paz las filas nulas."""
    normas = numpy.linalg.norm(matriz, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    return matriz / normas
