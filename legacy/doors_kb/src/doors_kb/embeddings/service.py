"""Generacion incremental de embeddings (RF-071 a RF-074, hito H5).

La regla que define este servicio es RF-074: **solo se generan embeddings para requisitos
nuevos o modificados**. Los que no han cambiado no se recalculan. Con un proveedor de pago
esa diferencia es directamente la factura, y con cualquier proveedor es el tiempo de una
reindexacion completa en cada sincronizacion.

Lo que hace posible esa regla ya estaba construido: el repositorio clasifica cada requisito
por hash al sincronizar, y aqui solo hay que comparar el hash del texto de embedding
guardado con el actual.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ..config import Settings
from ..db.repository import SqliteRepository
from ..errors import EmbeddingError
from .provider import EmbeddingProvider
from .text import construir_texto, hash_texto

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Mantiene al dia el indice de embeddings de un modulo."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        repository: SqliteRepository,
        settings: Settings | None = None,
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.settings = settings or Settings()

    def update_index(
        self, module_path: str, *, attributes: Sequence[str] | None = None
    ) -> dict[str, object]:
        """Genera los embeddings que faltan o han quedado obsoletos.

        Devuelve las estadisticas de la pasada. ``skipped`` es el numero de requisitos que
        no hizo falta reembeder: en una copia local estable deberia ser casi el total.
        """
        atributos = tuple(
            attributes if attributes is not None else self.settings.embeddings_attributes
        )
        modelo = self.provider.model
        run_id = self.repository.start_embedding_run(module_path, modelo)
        stats: dict[str, object] = {
            "module_path": module_path,
            "model": modelo,
            "candidates": 0,
            "generated": 0,
            "skipped": 0,
            "removed": 0,
            "error": None,
        }

        try:
            # Primero se limpia: un requisito borrado en DOORS no debe seguir apareciendo en
            # las busquedas semanticas mientras se regenera el resto.
            stats["removed"] = self.repository.borrar_embeddings_de_borrados(module_path, modelo)

            candidatos = self.repository.embeddings_pendientes(module_path, modelo)
            stats["candidates"] = len(candidatos)

            pendientes = []
            for requisito in candidatos:
                texto = construir_texto(requisito, atributos)
                hash_actual = hash_texto(texto)
                if requisito.get("embedding_text_hash_guardado") == hash_actual:
                    stats["skipped"] = int(stats["skipped"]) + 1
                    continue
                pendientes.append((requisito, texto, hash_actual))

            self._avisar_de_atributos_ausentes(atributos, candidatos)

            if pendientes:
                self._generar(pendientes, modelo)
                stats["generated"] = len(pendientes)

            self.repository.finish_embedding_run(run_id, "success", stats)
            logger.info("Embeddings de '%s' actualizados: %s", module_path, stats)
            return stats

        except Exception as exc:
            stats["error"] = f"{type(exc).__name__}: {exc}"
            self.repository.finish_embedding_run(run_id, "failed", stats)
            logger.error(
                "Generacion de embeddings fallida en '%s': %s", module_path, stats["error"]
            )
            raise

    @staticmethod
    def _avisar_de_atributos_ausentes(
        atributos: Sequence[str], candidatos: list[dict]
    ) -> None:
        """Avisa si un atributo del perfil no existe en la copia local.

        El perfil de embeddings solo puede usar atributos que la sincronizacion haya traido:
        pedir uno que no esta en DOORS_SYNC_ATTRIBUTES no da error, simplemente no aporta
        nada al texto. Sin este aviso seria un ajuste que parece aplicado y no lo esta.
        """
        if not atributos or not candidatos:
            return
        presentes = set()
        for requisito in candidatos:
            valores = requisito.get("attributes")
            if isinstance(valores, dict):
                presentes.update(valores)
        ausentes = [nombre for nombre in atributos if nombre not in presentes]
        if ausentes:
            logger.warning(
                "Estos atributos del perfil de embeddings no estan en la copia local y no "
                "aportan nada al texto: %s. Anadelos a DOORS_SYNC_ATTRIBUTES y vuelve a "
                "sincronizar.",
                ", ".join(ausentes),
            )

    def _generar(self, pendientes: list[tuple[dict, str, str]], modelo: str) -> None:
        """Pide los vectores al proveedor y los guarda en una sola transaccion.

        El proveedor decide como trocear en lotes; aqui se le pasa todo junto para que pueda
        aprovechar el tamano de lote configurado. La escritura va en una transaccion para no
        dejar la mitad de los embeddings guardados si algo falla al final.
        """
        textos = [texto for _, texto, _ in pendientes]
        vectores = self.provider.embed(textos)

        if len(vectores) != len(pendientes):
            raise EmbeddingError(
                f"El proveedor devolvio {len(vectores)} vectores para {len(pendientes)} "
                "textos: la respuesta no se puede asociar a los requisitos."
            )

        with self.repository.transaction():
            for (requisito, _, hash_texto_actual), vector in zip(pendientes, vectores, strict=True):
                self.repository.guardar_embedding(
                    int(requisito["id"]),
                    model=modelo,
                    content_hash=str(requisito["content_hash"]),
                    embedding_text_hash=hash_texto_actual,
                    vector=vector,
                )
