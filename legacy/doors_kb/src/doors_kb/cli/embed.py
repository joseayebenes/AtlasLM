"""Comando ``doors-embed``: genera los embeddings que faltan en la copia local.

Es el paso siguiente a ``doors-sync`` y se apoya en su trabajo: la sincronizacion ya dejo
clasificado que cambio, asi que aqui solo se embebe lo nuevo o lo modificado (RF-074).

Con ``--provider fake`` usa el proveedor determinista sin red, util para comprobar la
instalacion y ver el mecanismo completo sin gastar llamadas a la API.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from ..config import Settings
from ..db import SqliteRepository
from ..embeddings import EmbeddingService, FakeEmbeddingProvider, OpenAICompatibleProvider
from ..embeddings.provider import EmbeddingProvider
from ..errors import DoorsKbError

logger = logging.getLogger("doors_kb.cli.embed")


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doors-embed",
        description="Genera los embeddings pendientes de un modulo en la copia local.",
        epilog=(
            "Solo se embeben los requisitos nuevos o modificados. Una segunda ejecucion "
            "sin cambios no realiza ninguna llamada al proveedor."
        ),
    )
    parser.add_argument("--module", help="Ruta completa del modulo en DOORS.")
    parser.add_argument("--db", help="Ruta del fichero SQLite de la copia local.")
    parser.add_argument(
        "--attributes",
        help=(
            "Atributos que entran en el texto de embedding, separados por comas. Cambiarlos "
            "regenera todos los embeddings del modulo."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=("api", "fake"),
        default="api",
        help="Proveedor de embeddings. 'fake' es determinista y no necesita red.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def _crear_proveedor(ajustes: Settings, tipo: str) -> EmbeddingProvider:
    if tipo == "fake":
        return FakeEmbeddingProvider()
    return OpenAICompatibleProvider(
        ajustes.embeddings_base_url,
        ajustes.embeddings_api_key,
        ajustes.embeddings_model,
        timeout=ajustes.embeddings_timeout_seconds,
        batch_size=ajustes.embeddings_batch_size,
    )


def main(argv: list[str] | None = None) -> int:
    argumentos = construir_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if argumentos.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        base = Settings.from_env()
        cambios: dict[str, object] = {}
        if argumentos.module:
            cambios["module_path"] = argumentos.module
        if argumentos.db:
            cambios["db_path"] = argumentos.db
        if argumentos.attributes:
            cambios["embeddings_attributes"] = tuple(
                p.strip() for p in argumentos.attributes.split(",") if p.strip()
            )
        ajustes = Settings(**{**base.__dict__, **cambios})
        modulo = ajustes.resolve_module_path()
        proveedor = _crear_proveedor(ajustes, argumentos.provider)

        with SqliteRepository(ajustes.db_path) as repositorio:
            stats = EmbeddingService(proveedor, repositorio, ajustes).update_index(modulo)

        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    except DoorsKbError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
