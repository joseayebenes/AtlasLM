"""Carga y validación de atlas.toml.

- Un único fichero TOML; los valores admiten ${VAR} y ${VAR:-defecto}.
- Cada sección conocida tiene un esquema (clave -> tipo). Claves desconocidas o tipos
  incorrectos son errores con sección y clave.
- `fingerprint(seccion)` devuelve un hash estable de una sección: el motor incremental lo
  incluye en la huella de las unidades que dependen de esa sección.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def _expandir(v: Any) -> Any:
    if isinstance(v, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), v)
    if isinstance(v, dict):
        return {k: _expandir(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expandir(x) for x in v]
    return v


# Esquema: sección -> {clave: tipo | "any"}. "list" = lista, "dict" = tabla anidada libre.
ESQUEMA: dict[str, dict[str, Any]] = {
    "project": {"name": str, "workspace": str, "log_level": str},
    "llm": {"base_url": str, "model": str, "api_key": str, "temperature": float,
            "max_tokens": int, "network": dict},
    "embeddings": {"base_url": str, "model": str, "api_key": str, "batch": int, "network": dict},
    "network": {"proxy": str, "ca_cert": str, "client_cert": str, "client_key": str,
                "no_proxy": str, "insecure": bool, "timeout": float, "requests_per_minute": int,
                "tokens_per_minute": int, "adaptive": bool, "max_concurrent": int,
                "max_retries": int, "max_backoff": float, "rate_limit_max_wait": float,
                "retry_statuses": list},
    "pipeline": {"workers": int, "on_error": str, "max_attempts": int, "retry_quarantined": bool,
                 "prune_missing": bool, "steps": list},
    "plugins": {"modules": list, "disabled": list},
    "sources": {"docs": list, "doors": dict, "code": dict},
    "graph": {"exports": list, "communities": bool},
    "index": {"chunk_chars": int, "chunk_overlap": int, "kinds": list},
    "agents": {"model": str, "assistant": dict, "context_builder": dict, "impact_analyst": dict,
               "coverage_auditor": dict, "eval": dict},
    "mcp": {"enabled": bool, "tools": list},
}

ESQUEMA_DOCS = {"name": str, "paths": list, "patterns": list, "analysis": str, "req_regex": str,
                "structure": str, "heading_regex": str, "lang": str, "max_chars": int,
                "min_chars": int, "enabled": bool}
ESQUEMA_DOORS = {"adapter": str, "enabled": bool, "database": dict, "convention": dict,
                 "modules": list, "attributes": list}
ESQUEMA_CODE = {"roots": list, "languages": list, "exclude": list, "req_tag": str,
                "test_patterns": list, "summaries": str, "enabled": bool}

DEFAULTS: dict[str, dict[str, Any]] = {
    "project": {"name": "atlas", "workspace": "./workspace", "log_level": "INFO"},
    "pipeline": {"workers": 1, "on_error": "continue", "max_attempts": 3,
                 "retry_quarantined": False, "prune_missing": True,
                 "steps": ["docs", "doors", "code", "graph", "index"]},
    "plugins": {"modules": [], "disabled": []},
    "graph": {"exports": ["graphml", "json"], "communities": False},
    "index": {"chunk_chars": 1200, "chunk_overlap": 200,
              "kinds": ["chunk", "section_summary", "requirement", "definition", "code_doc"]},
    "mcp": {"enabled": False, "tools": []},
}


def _validar(datos: dict, esquema: dict, donde: str) -> None:
    for k, v in datos.items():
        if k not in esquema:
            raise ConfigError(f"[{donde}] clave desconocida: '{k}' (válidas: {', '.join(esquema)})")
        tipo = esquema[k]
        if tipo is dict:
            if not isinstance(v, dict):
                raise ConfigError(f"[{donde}] '{k}' debe ser una tabla")
        elif tipo is list:
            if not isinstance(v, list):
                raise ConfigError(f"[{donde}] '{k}' debe ser una lista")
        elif tipo is float:
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ConfigError(f"[{donde}] '{k}' debe ser un número")
        elif tipo is int:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ConfigError(f"[{donde}] '{k}' debe ser un entero")
        elif not isinstance(v, tipo):
            raise ConfigError(f"[{donde}] '{k}' debe ser de tipo {tipo.__name__}")


@dataclass
class Config:
    """Configuración validada. Las secciones se consultan como diccionarios (`cfg["llm"]`)
    y cada módulo construye su propio objeto a partir de la suya."""

    path: Path
    data: dict = field(default_factory=dict)

    def __getitem__(self, seccion: str) -> Any:
        return self.data[seccion]

    def get(self, seccion: str, default: Any = None) -> Any:
        return self.data.get(seccion, default)

    @property
    def workspace(self) -> Path:
        ws = Path(self.data["project"]["workspace"])
        return ws if ws.is_absolute() else (self.path.parent / ws).resolve()

    def fingerprint(self, *secciones: str) -> str:
        """Hash estable de una o varias secciones (p. ej. 'sources.docs', 'embeddings')."""
        partes = []
        for s in secciones:
            d: Any = self.data
            for trozo in s.split("."):
                d = d.get(trozo, None) if isinstance(d, dict) else None
            partes.append(json.dumps(d, sort_keys=True, ensure_ascii=False, default=str))
        return hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()[:16]


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"no existe el fichero de configuración {path}")
    try:
        with open(path, "rb") as f:
            datos = _expandir(tomllib.load(f))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"sintaxis TOML en {path}: {e}") from e

    for seccion in datos:
        if seccion not in ESQUEMA:
            raise ConfigError(f"sección desconocida [{seccion}] (válidas: {', '.join(ESQUEMA)})")
    for seccion, esquema in ESQUEMA.items():
        if seccion in datos:
            _validar(datos[seccion], esquema, seccion)
    fuentes = datos.get("sources", {})
    for i, d in enumerate(fuentes.get("docs", [])):
        _validar(d, ESQUEMA_DOCS, f"sources.docs[{i}]")
        if "name" not in d or "paths" not in d:
            raise ConfigError(f"[sources.docs[{i}]] faltan 'name' o 'paths'")
    if "doors" in fuentes:
        _validar(fuentes["doors"], ESQUEMA_DOORS, "sources.doors")
    if "code" in fuentes:
        _validar(fuentes["code"], ESQUEMA_CODE, "sources.code")

    for seccion, defaults in DEFAULTS.items():
        datos[seccion] = {**defaults, **datos.get(seccion, {})}
    datos.setdefault("sources", {})
    return Config(path=path, data=datos)
