"""Recorrido comentado del ciclo de sincronizacion, sin DOORS.

Es la forma mas rapida de entender el sistema: crea un modulo simulado, lo sincroniza a una
base SQLite temporal y provoca, uno a uno, los cinco casos que el sincronizador tiene que
distinguir. Cada paso imprime lo que ha pasado y por que importa.

    python examples/demo_sync_fake.py

No necesita Windows, ni DOORS, ni red.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from doors_kb.config import Settings
from doors_kb.db import SqliteRepository
from doors_kb.errors import SyncError
from doors_kb.models import AttributeDefinition
from doors_kb.sources.fake import FakeDoorsSource
from doors_kb.sync import SyncService

MODULO = "/Demo/Requisitos"
ATRIBUTOS = ("Object Heading", "Object Text", "Estado")


def titulo(texto: str) -> None:
    print(f"\n{'=' * 78}\n{texto}\n{'=' * 78}")


def resumen(etiqueta: str, stats) -> None:
    print(
        f"  {etiqueta:<28} inserted={stats.inserted}  updated={stats.updated}  "
        f"unchanged={stats.unchanged}  deleted={stats.deleted}"
    )


def crear_modulo() -> FakeDoorsSource:
    """Un modulo de DOORS simulado con tres requisitos."""
    origen = FakeDoorsSource(module_path=MODULO)
    origen.definir_atributo(AttributeDefinition("Estado", "Enumeration",
                                                enum_values=("Propuesto", "Aprobado")))
    origen.anadir(1, heading="Alcance", text="El sistema cubre la gestion de sesiones.",
                  attributes={"Estado": "Aprobado"})
    origen.anadir(2, heading="Timeout TCP", text="La conexion se cierra tras 30 segundos.",
                  attributes={"Estado": "Aprobado"})
    origen.anadir(3, heading="Registro", text="Los eventos se registran en disco.",
                  attributes={"Estado": "Propuesto"})
    return origen


def main() -> None:
    origen = crear_modulo()

    with tempfile.TemporaryDirectory() as carpeta:
        ruta = Path(carpeta) / "demo.sqlite3"
        ajustes = Settings(sync_attributes=ATRIBUTOS, sync_page_size=2, db_path=str(ruta))

        with SqliteRepository(ruta) as repo:
            sync = SyncService(origen, repo, ajustes)

            titulo("1. Primera sincronizacion: todo es nuevo")
            resumen("primera pasada", sync.sync_module(MODULO))
            print("  Los tres requisitos entran como 'inserted'. En el hito H5, estos son")
            print("  exactamente los que habria que embeder por primera vez.")

            titulo("2. Segunda sincronizacion sin cambios (criterio CA-002)")
            resumen("sin tocar nada", sync.sync_module(MODULO))
            print("  Cero escrituras utiles: el hash de contenido coincide, asi que no hay")
            print("  que regenerar ningun embedding.")

            titulo("3. Se modifica un requisito en DOORS (criterio CA-003)")
            origen.modificar(2, text="La conexion se cierra tras 60 segundos.")
            resumen("tras editar el REQ-2", sync.sync_module(MODULO))
            print("  Solo el objeto tocado cuenta como 'updated'. Los otros dos no se")
            print("  recalculan.")

            titulo("4. Se borra un requisito en DOORS (criterio CA-004)")
            origen.borrar(1)
            resumen("tras borrar el REQ-1", sync.sync_module(MODULO))
            print(f"  Sigue en la base con is_deleted=1: {repo.get_requirement(MODULO, 1) is None}")
            print("  El borrado es logico, nunca fisico: el requisito puede volver.")

            titulo("5. El requisito borrado reaparece con el MISMO contenido (RF-056)")
            origen.restaurar(1)
            resumen("tras restaurar el REQ-1", sync.sync_module(MODULO))
            print("  Cuenta como 'updated' aunque su hash no haya cambiado. Este es el caso")
            print("  que pierde una comparacion basada solo en el hash: se quedaria marcado")
            print("  como borrado para siempre e invisible en las busquedas locales.")

            titulo("6. Una sincronizacion se corta a mitad (criterio CA-005)")
            origen.pages_served = 0
            origen.fail_after_pages = 1  # falla al pedir la segunda pagina
            try:
                sync.sync_module(MODULO)
            except SyncError as exc:
                print(f"  Error controlado: {exc}")
            activos = repo.count_requirements(MODULO)
            print(f"  Requisitos activos despues del corte: {activos} (siguen los 3)")
            print("  Ningun requisito se ha marcado como eliminado: el marcado de ausentes")
            print("  exige haber recorrido el modulo ENTERO, no solo no haber fallado.")

            titulo("Estado final")
            print(f"  Frescura del modulo: {repo.module_freshness(MODULO)}")
            print(f"  Ultima sincronizacion: {repo.last_sync_run(MODULO)['status']}")


if __name__ == "__main__":
    main()
