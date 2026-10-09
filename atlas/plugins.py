"""Registro de plugins: todo lo que se pueda añadir o quitar pasa por aquí.

Tipos de plugin (clase base -> nombre del registro):
    CodeAnalyzer          "code_analyzer"      un lenguaje de programación
    RequirementsAdapter   "requirements"       una forma de leer requisitos (BD, ReqIF, CSV, MCP)
    DocumentExtractor     "extractor"          una forma de sacar texto de un documento (PDF, DOCX...)
    Tool                  "tool"               una herramienta para agentes ADK y para el servidor MCP

Cómo se registran:
    1. Los integrados: cada módulo de atlas llama a `register(...)` al importarse.
    2. Externos por entry point: en el pyproject del paquete externo,
         [project.entry-points."atlas.plugins"]  mi_plugin = "mi_paquete.mi_modulo"
       y ese módulo registra lo que ofrece al importarse.
    3. Por configuración: [plugins] modules = ["mi_paquete.mi_modulo"] (misma idea, sin instalar).
    4. En Python: atlas.plugins.register("code_analyzer", "cpp", MiAnalizador).

Cómo se quitan: [plugins] disabled = ["code_analyzer:cpp"], o no activándolos en la
configuración de la fuente (p. ej. languages = ["ada"] solo usa el analizador "ada").
"""

from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

TIPOS = ("code_analyzer", "requirements", "extractor", "tool")


# ----------------------------------------------------------------------------
# Interfaces
# ----------------------------------------------------------------------------

@dataclass
class CodeUnit:
    """Resultado del análisis de un fichero de código: lo mínimo que el grafo necesita."""
    file: str
    unit: str                                   # nombre de la unidad de compilación / módulo
    language: str
    subprograms: list[dict] = field(default_factory=list)   # {name, start_line, end_line, calls, doc, reqs}
    types: list[dict] = field(default_factory=list)
    variables: list[dict] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)        # withs / includes
    tests: list[dict] = field(default_factory=list)         # {name, covers:[...], reqs:[...]}


class CodeAnalyzer(ABC):
    """Un analizador por lenguaje. Debe ser determinista y trabajar fichero a fichero
    (es la unidad del motor incremental)."""
    name: str = ""                # "ada", "cpp"...
    extensions: tuple[str, ...] = ()

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def analyze_file(self, path: Path, source: bytes) -> CodeUnit: ...

    def version(self) -> str:
        """Cambiarla invalida los resultados cacheados de este analizador."""
        return "1"


@dataclass
class Requirement:
    id: str                       # ya normalizado según la convención configurada
    text: str
    module: str
    attributes: dict = field(default_factory=dict)
    parent: str | None = None     # jerarquía dentro del módulo (objeto padre), si existe
    raw_id: str = ""              # tal como venía en la fuente


@dataclass
class Link:
    source: str
    target: str
    type: str                     # satisfies, derives_from, refines, verifies...


class RequirementsAdapter(ABC):
    """Lee requisitos y enlaces de una fuente. `snapshot()` devuelve algo cuyo hash cambia
    cuando cambia la fuente (p. ej. fecha de exportación o hash de la tabla), para que el
    motor sepa si hay que releer."""
    name: str = ""

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def modules(self) -> list[str]: ...

    @abstractmethod
    def snapshot(self, module: str) -> str: ...

    @abstractmethod
    def requirements(self, module: str) -> Iterable[Requirement]: ...

    @abstractmethod
    def links(self, module: str) -> Iterable[Link]: ...


class DocumentExtractor(ABC):
    """Convierte un documento en texto (con \\f entre páginas si las hay)."""
    name: str = ""
    extensions: tuple[str, ...] = ()

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def extract(self, path: Path) -> str: ...


class Tool(ABC):
    """Herramienta para agentes ADK y para el servidor MCP. `run` recibe argumentos con
    nombre y devuelve algo serializable a JSON y compacto (usa `truncated` si recorta)."""
    name: str = ""
    description: str = ""

    def __init__(self, context: Any):
        self.context = context      # acceso a grafo, índice, requisitos, código

    @abstractmethod
    def run(self, **kwargs) -> Any: ...


# ----------------------------------------------------------------------------
# Registro
# ----------------------------------------------------------------------------

_REGISTRO: dict[str, dict[str, type]] = {t: {} for t in TIPOS}
_CARGADOS = False


def register(tipo: str, nombre: str, cls: type) -> type:
    if tipo not in TIPOS:
        raise ValueError(f"tipo de plugin desconocido '{tipo}' (válidos: {', '.join(TIPOS)})")
    if nombre in _REGISTRO[tipo] and _REGISTRO[tipo][nombre] is not cls:
        log.warning("plugin %s:%s redefinido por %s", tipo, nombre, cls)
    _REGISTRO[tipo][nombre] = cls
    return cls


def plugin(tipo: str, nombre: str) -> Callable[[type], type]:
    """Decorador: @plugin("code_analyzer", "ada")"""
    return lambda cls: register(tipo, nombre, cls)


def available(tipo: str) -> list[str]:
    return sorted(_REGISTRO[tipo])


def get(tipo: str, nombre: str, disabled: Iterable[str] = ()) -> type:
    if f"{tipo}:{nombre}" in set(disabled):
        raise LookupError(f"el plugin {tipo}:{nombre} está desactivado en [plugins] disabled")
    try:
        return _REGISTRO[tipo][nombre]
    except KeyError:
        raise LookupError(f"no hay plugin {tipo} llamado '{nombre}'; disponibles: "
                          f"{', '.join(available(tipo)) or 'ninguno'}") from None


def load_plugins(modules: Iterable[str] = (), use_entry_points: bool = True) -> list[str]:
    """Importa los módulos integrados, los declarados por entry point y los de la
    configuración. Devuelve los nombres cargados."""
    global _CARGADOS
    cargados: list[str] = []
    integrados = ["atlas.code", "atlas.doors", "atlas.docs"]
    for m in integrados:
        try:
            importlib.import_module(m)
            cargados.append(m)
        except ImportError as e:               # dependencia opcional ausente: se sigue
            log.info("plugin integrado %s no disponible: %s", m, e)
    if use_entry_points:
        for ep in entry_points(group="atlas.plugins"):
            try:
                ep.load()
                cargados.append(f"entry-point:{ep.name}")
            except Exception as e:
                log.warning("no se pudo cargar el plugin %s: %s", ep.name, e)
    for m in modules:
        importlib.import_module(m)              # un fallo aquí sí es error: lo pidió el usuario
        cargados.append(m)
    _CARGADOS = True
    return cargados


def describe() -> dict[str, list[str]]:
    return {t: available(t) for t in TIPOS}
