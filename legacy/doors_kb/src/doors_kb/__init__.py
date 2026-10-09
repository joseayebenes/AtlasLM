"""Capa de acceso para IA sobre IBM DOORS Classic.

El paquete se divide en cuatro capas con fronteras explicitas (ADR-008):

* ``sources``  -- de donde salen los requisitos (DOORS por COM/DXL, o una fuente falsa).
* ``db``       -- donde se guardan localmente (SQLite).
* ``sync``     -- como se copian de forma segura e incremental de lo primero a lo segundo.
* ``embeddings`` -- como se convierten en vectores para la busqueda semantica.
* ``search``   -- como se encuentran: textual, semantica e hibrida.
* ``servers``  -- como los consulta un agente de IA (servidores MCP).

Importar este paquete no requiere Windows ni DOORS: el cliente COM se importa de forma
perezosa dentro de ``sources.doors`` (RNF-013).
"""

__version__ = "0.4.0"
