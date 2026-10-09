"""Busqueda sobre la copia local.

Tres modos, en el orden en que los introduce el roadmap:

* ``lexical``  -- FTS5 sobre el texto (hito H4). Rapido y exacto para identificadores y
  vocabulario tecnico, que es donde la busqueda semantica rinde peor (ADR-007).
* ``vector``   -- similitud de embeddings (hito H5/H6). Encuentra por significado cuando las
  palabras no coinciden.
* ``hybrid``   -- fusion de los dos rankings (hito H6).

Ninguno consulta DOORS: todos trabajan sobre la copia local, que es el objetivo de la
segunda fase del proyecto.
"""

from .hybrid import ResultadoHibrido, buscar_hibrida
from .lexical import ResultadoBusqueda, buscar_lexical
from .vector import ResultadoVectorial, buscar_vectorial

__all__ = [
    "ResultadoBusqueda",
    "ResultadoHibrido",
    "ResultadoVectorial",
    "buscar_hibrida",
    "buscar_lexical",
    "buscar_vectorial",
]
