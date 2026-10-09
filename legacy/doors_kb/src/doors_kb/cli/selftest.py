"""Comando ``doors-selftest``: comprueba las primitivas de DXL contra tu DOORS.

Ejecuta cada prueba de ``sources.doors.selftest`` por separado y muestra cuales funcionan.
Sirve para validar de una vez las suposiciones sobre el lenguaje que no se pueden comprobar
fuera de Windows, en lugar de descubrirlas de una en una a base de sincronizaciones
fallidas.

Cada prueba es independiente: que una falle no impide que las demas se ejecuten.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from ..config import Settings
from ..errors import DoorsKbError
from ..sources.doors import selftest
from ..sources.doors.client import DoorsComClient

logger = logging.getLogger("doors_kb.cli.selftest")


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doors-selftest",
        description=(
            "Comprueba una por una las primitivas de DXL que usa el proyecto contra tu "
            "instalacion de DOORS."
        ),
        epilog=(
            "Abre la ventana 'DXL output' de DOORS antes de ejecutarlo: ahi aparece el "
            "mensaje del interprete de cada prueba que falle."
        ),
    )
    parser.add_argument("--module", help="Modulo sobre el que hacer las comprobaciones.")
    parser.add_argument(
        "--json", action="store_true", help="Salida en JSON en lugar de tabla."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argumentos = construir_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")

    try:
        ajustes = Settings.from_env()
        modulo = ajustes.resolve_module_path(argumentos.module)
        cliente = DoorsComClient(ajustes)
        print(
            "Abriendo la sesion Automation de DOORS. Si aparece la ventana de inicio de "
            "sesion, autenticate en ELLA.",
            file=sys.stderr,
        )
        cliente.start_session()
    except DoorsKbError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    resultados = []
    try:
        for prueba in selftest.PRUEBAS:
            testigo = cliente.nuevo_testigo()
            script = selftest.construir_script(prueba, modulo, testigo)
            try:
                crudo = cliente._ejecutar_dxl(script)
                funciona, detalle = selftest.interpretar(prueba, crudo, testigo)
            except Exception as exc:
                funciona, detalle = False, f"{type(exc).__name__}: {exc}"
            resultados.append(
                {
                    "prueba": prueba.nombre,
                    "funciona": funciona,
                    "detalle": detalle,
                    "porque_importa": prueba.porque_importa,
                }
            )
    finally:
        cliente.close()

    if argumentos.json:
        print(json.dumps({"module_path": modulo, "results": resultados}, ensure_ascii=False,
                         indent=2))
    else:
        _imprimir_tabla(modulo, resultados)

    fallidas = [r for r in resultados if not r["funciona"]]
    return 1 if fallidas else 0


def _imprimir_tabla(modulo: str, resultados: list[dict]) -> None:
    ancho = max(len(r["prueba"]) for r in resultados)
    print(f"\nModulo: {modulo}\n")
    for resultado in resultados:
        marca = "OK  " if resultado["funciona"] else "FALLA"
        print(f"  {marca} {resultado['prueba']:<{ancho}}  {resultado['detalle']}")

    fallidas = [r for r in resultados if not r["funciona"]]
    print(f"\n{len(resultados) - len(fallidas)} de {len(resultados)} funcionan.")
    if fallidas:
        # Para cada fallo se explica que capacidad del proyecto queda afectada: sin eso, la
        # tabla dice que algo no va pero no que consecuencias tiene.
        print("\nLo que no funciona y a que afecta:\n")
        for resultado in fallidas:
            print(f"  - {resultado['prueba']}: {resultado['porque_importa']}")
        print(
            "\nEl mensaje del interprete DXL de cada fallo esta en la ventana 'DXL output' "
            "de DOORS."
        )


if __name__ == "__main__":
    raise SystemExit(main())
