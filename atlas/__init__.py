"""atlas — engineering knowledge base (documents, requirements, code and graph).

Quick use:
    from atlas import load
    cfg = load("atlas.dev.toml")       # validated configuration
"""
from atlas.config import load_config as load  # noqa: F401

__version__ = "0.2.0"
