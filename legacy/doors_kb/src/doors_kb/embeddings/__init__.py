"""Generacion de embeddings de los requisitos (hito H5).

Tres piezas separadas por lo que cambia en cada una:

* ``text``     -- que texto representa a un requisito (RF-072). Cambia con el proyecto.
* ``provider`` -- quien convierte ese texto en un vector (RF-071). Cambia con el proveedor.
* ``service``  -- que requisitos hay que reembeder y cuando (RF-073, RF-074). No cambia.
"""

from .provider import EmbeddingProvider, FakeEmbeddingProvider, OpenAICompatibleProvider
from .service import EmbeddingService
from .text import construir_texto, hash_texto

__all__ = [
    "EmbeddingProvider",
    "EmbeddingService",
    "FakeEmbeddingProvider",
    "OpenAICompatibleProvider",
    "construir_texto",
    "hash_texto",
]
