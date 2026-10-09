"""Recorrido comentado de la busqueda local: textual, semantica e hibrida.

Continua donde termina ``demo_sync_fake.py``: parte de una copia local ya sincronizada y
muestra que aporta cada modo de busqueda, y donde falla cada uno.

    python examples/demo_busqueda_hibrida.py

No necesita DOORS, ni Windows, ni red: usa el proveedor de embeddings determinista. Ese
proveedor **no es un modelo semantico**, asi que lo que se ve aqui es el mecanismo -como se
combinan los rankings y por que-, no la calidad semantica real, que solo se puede valorar
con el proveedor de produccion.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from doors_kb.config import Settings
from doors_kb.db import SqliteRepository
from doors_kb.embeddings import EmbeddingService, FakeEmbeddingProvider
from doors_kb.models import AttributeDefinition
from doors_kb.search import buscar_hibrida, buscar_lexical, buscar_vectorial
from doors_kb.sources.fake import FakeDoorsSource
from doors_kb.sync import SyncService

MODULO = "/Demo/Requisitos"
ATRIBUTOS = ("Object Heading", "Object Text", "Criticidad")

REQUISITOS = [
    (1, "Cierre de sesion TCP", "La conexion se cierra automaticamente tras 30 segundos.", "alta"),
    (2, "Registro de eventos", "Los eventos del sistema se almacenan en disco.", "media"),
    (3, "Cifrado del canal", "El canal usa TLS 1.3 en todas las comunicaciones.", "alta"),
    (4, "Identificador REQ-SEC-042", "El modulo criptografico cumple la norma indicada.", "alta"),
    (5, "Arranque", "El sistema esta operativo en menos de 10 segundos.", "baja"),
]


def titulo(texto: str) -> None:
    print(f"\n{'=' * 78}\n{texto}\n{'=' * 78}")


def mostrar(resultados) -> None:
    if not resultados:
        print("   (sin resultados)")
        return
    for resultado in resultados:
        datos = resultado.to_dict()
        procedencia = datos.get("matched_by")
        etiqueta = f"  [{'+'.join(procedencia)}]" if procedencia else ""
        print(f"   #{datos['absolute_number']} {datos['heading']}{etiqueta}")


def preparar(repo: SqliteRepository) -> FakeEmbeddingProvider:
    origen = FakeDoorsSource(module_path=MODULO)
    origen.definir_atributo(AttributeDefinition("Criticidad", "String"))
    for numero, titulo_req, texto, criticidad in REQUISITOS:
        origen.anadir(numero, heading=titulo_req, text=texto,
                      attributes={"Criticidad": criticidad})

    ajustes = Settings(sync_attributes=ATRIBUTOS, embeddings_attributes=("Criticidad",))
    SyncService(origen, repo, ajustes).sync_module(MODULO)

    proveedor = FakeEmbeddingProvider()
    estadisticas = EmbeddingService(proveedor, repo, ajustes).update_index(MODULO)
    print(f"   Copia local lista: {estadisticas['generated']} embeddings generados.")
    return proveedor


def main() -> None:
    with tempfile.TemporaryDirectory() as carpeta:
        with SqliteRepository(Path(carpeta) / "kb.sqlite3") as repo:
            titulo("0. Preparacion: sincronizar e indexar")
            proveedor = preparar(repo)

            titulo("1. Busqueda textual: imbatible con identificadores exactos")
            consulta = "REQ-SEC-042"
            print(f"   Consulta: '{consulta}'")
            mostrar(buscar_lexical(repo, consulta, module_path=MODULO))
            print("   FTS encuentra el codigo literal sin ambiguedad y sin coste alguno.")
            print("   Es la razon por la que ADR-007 pide tener FTS antes que embeddings.")

            titulo("2. ...pero no encuentra lo que se describe con otras palabras")
            consulta = "cuanto tarda en cerrarse la conexion"
            print(f"   Consulta: '{consulta}'")
            print("   Solo textual:")
            mostrar(buscar_lexical(repo, consulta, module_path=MODULO))
            print("   Ninguna palabra de la consulta aparece literalmente en el requisito.")

            titulo("3. La busqueda semantica cubre ese hueco")
            print("   Solo semantica:")
            mostrar(buscar_vectorial(repo, consulta, proveedor, module_path=MODULO, limit=3))

            titulo("4. La hibrida combina ambas y explica por que aparece cada resultado")
            print("   Consulta: 'cierre de la conexion TCP'")
            mostrar(
                buscar_hibrida(repo, "cierre de la conexion TCP", proveedor,
                               module_path=MODULO, limit=3)
            )
            print("   Lo que sale por las dos vias sube en el ranking: el acuerdo refuerza.")

            titulo("5. Los filtros estructurados se aplican antes de puntuar")
            print("   Consulta: 'seguridad del canal', filtrada por Criticidad=alta")
            mostrar(
                buscar_hibrida(repo, "seguridad del canal", proveedor, module_path=MODULO,
                               filtros_atributos={"Criticidad": "alta"}, limit=3)
            )
            print("   Filtrar despues del top-k devolveria menos resultados de los pedidos.")

            titulo("6. Frescura: la copia local sabe a que fecha responde")
            print(f"   {repo.module_freshness(MODULO)}")
            print("   Cada respuesta del servidor MCP local incluye este dato (riesgo R-007).")


if __name__ == "__main__":
    main()
