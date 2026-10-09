"""Acceso real a DOORS Classic mediante Automation (COM) y DXL.

Este subpaquete es el unico que depende de Windows. Se divide a proposito en tres piezas
con niveles de dependencia distintos:

* ``dxl``        -- genera y escapa scripts DXL. Codigo puro: se prueba en cualquier
                    plataforma, y ahi viven las regresiones conocidas (RNF-008, RF-041).
* ``com_worker`` -- serializa las llamadas COM en un hilo con timeouts y reintentos. Importa
                    pywin32 de forma perezosa, asi que su logica tambien se prueba fuera de
                    Windows inyectando funciones falsas.
* ``client``     -- une ambas y ofrece un ``RequirementsSource`` real. Solo funciona con
                    DOORS instalado.

Importar este modulo **no** requiere pywin32; solo lo requiere crear un ``DoorsComClient``.
"""

from .dxl import build_preamble, escape_dxl_string

__all__ = ["build_preamble", "escape_dxl_string"]
