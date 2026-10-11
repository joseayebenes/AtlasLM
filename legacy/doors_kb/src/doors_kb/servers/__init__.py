"""Servidores MCP.

Hay dos, y estan separados a proposito (seccion 5.1, hitos H1 y H7):

* ``doors_server`` -- acceso directo a DOORS. Informacion siempre actual, latencia alta,
  requiere Windows y DOORS instalado. Implementado.
* ``kb_server``    -- consultas sobre la copia local SQLite: textual, semantica e hibrida.
  Latencia baja y busquedas masivas, sin necesidad de DOORS ni de Windows.

Mantenerlos separados evita que la Knowledge Base quede atada a Windows en la practica.
"""
