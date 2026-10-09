"""Comando ``doors-search``: consulta la copia local desde la terminal.

Sirve para dos cosas: comprobar de extremo a extremo que el indice y los embeddings
funcionan, y comparar los tres modos sobre las mismas consultas, que es como se decide si la
busqueda semantica esta aportando algo (ADR-007).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from ..config import Settings
from ..db import SqliteRepository
from ..errors import DoorsKbError
from ..search import buscar_hibrida, buscar_lexical, buscar_vectorial
from .embed import _crear_proveedor

logger = logging.getLogger("doors_kb.cli.search")


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doors-search",
        description="Busca requisitos en la copia local, sin consultar DOORS.",
    )
    parser.add_argument("query", help="Texto a buscar.")
    parser.add_argument("--module", help="Ruta completa del modulo.")
    parser.add_argument("--db", help="Ruta del fichero SQLite de la copia local.")
    parser.add_argument(
        "--mode",
        choices=("lexical", "semantic", "hybrid"),
        default="lexical",
        help=(
            "lexical: FTS5, exacta y sin coste. semantic: por significado, necesita "
            "embeddings. hybrid: fusion de ambas."
        ),
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--provider",
        choices=("api", "fake"),
        default="api",
        help="Proveedor de embeddings para los modos semantic e hybrid.",
    )
    parser.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="ATRIBUTO=VALOR",
        help="Filtro estructurado, repetible. Ejemplo: --filter Criticidad=alta",
    )
    return parser


def _parsear_filtros(entradas: list[str]) -> dict[str, str]:
    filtros = {}
    for entrada in entradas:
        if "=" not in entrada:
            raise DoorsKbError(
                f"Filtro mal formado: '{entrada}'. Usa el formato ATRIBUTO=VALOR."
            )
        nombre, valor = entrada.split("=", 1)
        filtros[nombre.strip()] = valor.strip()
    return filtros


def main(argv: list[str] | None = None) -> int:
    argumentos = construir_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    try:
        base = Settings.from_env()
        cambios: dict[str, object] = {}
        if argumentos.module:
            cambios["module_path"] = argumentos.module
        if argumentos.db:
            cambios["db_path"] = argumentos.db
        ajustes = Settings(**{**base.__dict__, **cambios})
        modulo = ajustes.resolve_module_path()
        filtros = _parsear_filtros(argumentos.filter)

        with SqliteRepository(ajustes.db_path) as repositorio:
            if argumentos.mode == "lexical":
                resultados = [
                    r.to_dict()
                    for r in buscar_lexical(
                        repositorio, argumentos.query, module_path=modulo, limit=argumentos.limit
                    )
                ]
            else:
                proveedor = _crear_proveedor(ajustes, argumentos.provider)
                busqueda = buscar_vectorial if argumentos.mode == "semantic" else buscar_hibrida
                resultados = [
                    r.to_dict()
                    for r in busqueda(
                        repositorio,
                        argumentos.query,
                        proveedor,
                        module_path=modulo,
                        filtros_atributos=filtros,
                        limit=argumentos.limit,
                    )
                ]

            frescura = repositorio.module_freshness(modulo)

        # La frescura acompana al resultado tambien aqui: quien lee esto en una terminal
        # necesita saber a que fecha corresponde igual que lo necesita un agente (R-007).
        print(
            json.dumps(
                {
                    "query": argumentos.query,
                    "mode": argumentos.mode,
                    "results": resultados,
                    "freshness": frescura,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    except DoorsKbError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
