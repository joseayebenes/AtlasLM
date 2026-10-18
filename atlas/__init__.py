"""atlas — engineering knowledge base (documents, requirements, code and graph).

Quick use:
    from atlas import load
    cfg = load("atlas.dev.toml")       # validated configuration

The public API, what is kept between versions, is in docs/api.md.
"""
from atlas.config import Config, ConfigError, load_config as load

__all__ = ["Config", "ConfigError", "__version__", "load"]

__version__ = "0.3.0"
