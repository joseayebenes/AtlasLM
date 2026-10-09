"""atlas — base de conocimiento de ingeniería (documentos, requisitos, código y grafo).

Uso rápido:
    from atlas import load
    cfg = load("atlas.dev.toml")       # configuración validada
"""
from atlas.config import load_config as load  # noqa: F401

__version__ = "0.0.1"
