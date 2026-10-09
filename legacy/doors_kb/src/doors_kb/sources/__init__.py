"""Fuentes de requisitos.

Una *fuente* es cualquier cosa capaz de devolver requisitos de un modulo: DOORS a traves de
COM/DXL (``doors``) o una simulacion en memoria (``fake``). El protocolo comun esta en
``base`` y es la frontera que aisla al resto del sistema de Windows y de DXL (ADR-008).
"""

from .base import RequirementsSource
from .fake import FakeDoorsSource

__all__ = ["RequirementsSource", "FakeDoorsSource"]
