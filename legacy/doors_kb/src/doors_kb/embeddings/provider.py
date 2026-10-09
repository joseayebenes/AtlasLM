"""Proveedores de embeddings (RF-071, ADR-009).

El proveedor de produccion habla el **protocolo de OpenAI** (``POST {base_url}/embeddings``),
que es lo que expone la API de terceros elegida para el proyecto. Se implementa sobre
``urllib.request`` de la biblioteca estandar, sin dependencias nuevas: el SDK de MCP arrastra
``httpx2``, no ``httpx``, y colgarse de una dependencia transitiva suya seria fragil.

El transporte HTTP se inyecta. No es un adorno de testabilidad: permite probar los lotes,
los reintentos y el tratamiento de errores sin red, igual que el worker COM se prueba sin
Windows inyectando la inicializacion de COM.

``FakeEmbeddingProvider`` genera vectores deterministas sin red, para tests y demos.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from ..errors import EmbeddingError

logger = logging.getLogger(__name__)

# Codigos que merecen reintento: limite de peticiones y fallos del servidor. Un 401 o un 400
# no mejoran repitiendo la llamada.
CODIGOS_REINTENTABLES = frozenset({408, 429, 500, 502, 503, 504})

# (codigo, cuerpo) que devuelve un transporte.
Transporte = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Convierte textos en vectores."""

    @property
    def model(self) -> str:
        """Identificador del modelo, que se guarda junto a cada embedding (RF-073)."""
        ...

    def embed(self, textos: Sequence[str]) -> list[list[float]]:
        """Devuelve un vector por texto, en el mismo orden."""
        ...


def _transporte_urllib(
    url: str, cuerpo: bytes, cabeceras: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    """Transporte por defecto: una peticion POST con la biblioteca estandar."""
    peticion = urllib.request.Request(url, data=cuerpo, headers=cabeceras, method="POST")
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            return respuesta.status, respuesta.read()
    except urllib.error.HTTPError as exc:
        # Un error HTTP tambien es una respuesta: su codigo decide si se reintenta.
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise EmbeddingError(
            f"No se pudo contactar con el servicio de embeddings ({url}): {exc.reason}. "
            "Comprueba EMBEDDINGS_BASE_URL y que la maquina tiene salida hacia ese endpoint."
        ) from exc


class OpenAICompatibleProvider:
    """Cliente de una API de embeddings compatible con el protocolo de OpenAI."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        batch_size: int = 32,
        reintentos: int = 3,
        espera_inicial: float = 1.0,
        transporte: Transporte = _transporte_urllib,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url:
            raise EmbeddingError(
                "Falta EMBEDDINGS_BASE_URL: la URL base del servicio de embeddings "
                "(por ejemplo https://mi-proveedor/v1)."
            )
        if not model:
            raise EmbeddingError("Falta EMBEDDINGS_MODEL: el nombre del modelo de embeddings.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._model = model
        self.timeout = timeout
        self.batch_size = batch_size
        self.reintentos = reintentos
        self.espera_inicial = espera_inicial
        self._transporte = transporte
        self._dormir = dormir

    @property
    def model(self) -> str:
        return self._model

    def embed(self, textos: Sequence[str]) -> list[list[float]]:
        """Genera los vectores, troceando en lotes del tamano configurado."""
        vectores: list[list[float]] = []
        for inicio in range(0, len(textos), self.batch_size):
            lote = list(textos[inicio : inicio + self.batch_size])
            if lote:
                vectores.extend(self._embed_lote(lote))
        return vectores

    def _embed_lote(self, lote: list[str]) -> list[list[float]]:
        url = f"{self.base_url}/embeddings"
        cuerpo = json.dumps({"model": self._model, "input": lote}).encode("utf-8")
        cabeceras = {"Content-Type": "application/json"}
        if self.api_key:
            cabeceras["Authorization"] = f"Bearer {self.api_key}"

        espera = self.espera_inicial
        for intento in range(1, self.reintentos + 1):
            codigo, respuesta = self._transporte(url, cuerpo, cabeceras, self.timeout)
            if codigo == 200:
                return self._extraer_vectores(respuesta, esperados=len(lote))
            if codigo in CODIGOS_REINTENTABLES and intento < self.reintentos:
                logger.warning(
                    "El servicio de embeddings respondio %d (intento %d de %d); "
                    "reintentando en %.1f s",
                    codigo,
                    intento,
                    self.reintentos,
                    espera,
                )
                self._dormir(espera)
                espera *= 2
                continue
            # El cuerpo del error se incluye recortado: suele decir exactamente que pasa
            # (modelo inexistente, clave invalida, entrada demasiado larga).
            raise EmbeddingError(
                f"El servicio de embeddings devolvio HTTP {codigo}: "
                f"{respuesta[:500].decode('utf-8', 'replace')}"
            )
        raise AssertionError("inalcanzable")  # pragma: no cover

    @staticmethod
    def _extraer_vectores(respuesta: bytes, *, esperados: int) -> list[list[float]]:
        """Parsea la respuesta y comprueba que viene completa y en orden.

        El campo ``index`` se respeta explicitamente en lugar de fiarse del orden de la
        lista: el protocolo no garantiza que venga ordenada, y un desajuste silencioso
        asociaria cada vector al requisito equivocado, que es un fallo practicamente
        imposible de detectar despues.
        """
        try:
            datos = json.loads(respuesta)
        except json.JSONDecodeError as exc:
            raise EmbeddingError(
                f"El servicio de embeddings devolvio una respuesta que no es JSON: "
                f"{respuesta[:300].decode('utf-8', 'replace')!r}"
            ) from exc

        elementos = datos.get("data")
        if not isinstance(elementos, list) or len(elementos) != esperados:
            raise EmbeddingError(
                f"El servicio de embeddings devolvio {len(elementos or [])} vectores para "
                f"{esperados} textos. La respuesta esta incompleta y no se puede asociar."
            )

        ordenados: list[list[float] | None] = [None] * esperados
        for elemento in elementos:
            indice = int(elemento.get("index", -1))
            if not 0 <= indice < esperados:
                raise EmbeddingError(
                    f"El servicio de embeddings devolvio un indice fuera de rango: {indice}."
                )
            ordenados[indice] = [float(x) for x in elemento["embedding"]]
        if any(v is None for v in ordenados):
            raise EmbeddingError(
                "El servicio de embeddings no devolvio un vector para cada texto."
            )
        return [v for v in ordenados if v is not None]


class FakeEmbeddingProvider:
    """Proveedor determinista sin red, para tests y demos.

    **No es un modelo semantico.** Proyecta el texto sobre un espacio fijo usando trigramas
    de caracteres y palabras (el "truco del hashing"), de modo que textos parecidos dan
    vectores parecidos. Sirve para ejercitar el indice, la generacion incremental y la
    fusion de rankings sin depender de una API externa, pero la calidad semantica real solo
    se puede valorar con el proveedor de produccion.
    """

    def __init__(self, model: str = "fake-embeddings-64", dim: int = 64) -> None:
        self._model = model
        self.dim = dim

    @property
    def model(self) -> str:
        return self._model

    def embed(self, textos: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in textos]

    def _vector(self, texto: str) -> list[float]:
        normalizado = texto.lower()
        piezas = normalizado.split()
        piezas += [normalizado[i : i + 3] for i in range(max(0, len(normalizado) - 2))]

        vector = [0.0] * self.dim
        for pieza in piezas:
            vector[hash_estable(pieza) % self.dim] += 1.0

        norma = math.sqrt(sum(x * x for x in vector))
        return [x / norma for x in vector] if norma else vector


def hash_estable(texto: str) -> int:
    """Hash reproducible entre ejecuciones.

    ``hash()`` de Python esta aleatorizado por proceso (PYTHONHASHSEED), asi que daria
    vectores distintos en cada arranque y los embeddings guardados dejarian de ser
    comparables con los recien generados.
    """
    import hashlib

    return int.from_bytes(hashlib.sha256(texto.encode("utf-8")).digest()[:8], "big")
