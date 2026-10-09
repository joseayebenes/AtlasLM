"""Persistencia local en SQLite.

La copia local es un **derivado reconstruible** de DOORS (ADR-004, RF-080): puede borrarse
y regenerarse con una sincronizacion completa. Nunca es la fuente de verdad.
"""

from .repository import SqliteRepository

__all__ = ["SqliteRepository"]
