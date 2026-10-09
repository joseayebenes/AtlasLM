"""Utilidades compartidas por los tests.

Todo lo que hay aqui funciona sin DOORS y sin Windows (RNF-013).
"""

import pytest

from doors_kb.models import AttributeDefinition
from doors_kb.sources.fake import FakeDoorsSource


@pytest.fixture
def atributos() -> tuple[str, ...]:
    """Perfil de atributos de las pruebas.

    Los dos atributos de contenido por defecto (RF-014) mas uno propio del proyecto, que
    ejercita el almacenamiento normalizado de atributos arbitrarios (RF-053).
    """
    return ("Object Heading", "Object Text", "Estado")


@pytest.fixture
def fuente() -> FakeDoorsSource:
    """Modulo simulado con tres requisitos y un atributo de proyecto."""
    origen = FakeDoorsSource(module_path="/Demo/Reqs")
    origen.definir_atributo(
        AttributeDefinition(
            "Estado", "Enumeration", enum_values=("Propuesto", "Aprobado", "Rechazado")
        )
    )
    origen.anadir(1, heading="Alcance", text="El sistema cubre la gestion de sesiones.",
                  attributes={"Estado": "Aprobado"})
    origen.anadir(2, heading="Timeout TCP", text="La conexion se cierra tras 30 segundos.",
                  attributes={"Estado": "Aprobado"})
    origen.anadir(3, heading="Registro", text="Los eventos se registran en disco.",
                  attributes={"Estado": "Propuesto"})
    return origen
