"""Conexión con las APIs de LLM y embeddings (proxy, TLS, límites, reintentos).

Es el `conexion.py` original, sin cambios de API. En atlas la configuración llega desde
las secciones [llm], [embeddings] y [network] de atlas.toml a través de `desde_config`.
"""
from __future__ import annotations

from dataclasses import fields, replace

from atlas.llm.conexion import (  # noqa: F401
    Conexion, NetConfig, ServiceConfig, comprobar, crear_cliente, describir, diagnosticar,
    es_error_conexion, limitador_para, mostrar, ocultar_credenciales,
)


def desde_config(cfg) -> Conexion:
    """Construye la Conexion a partir de atlas.toml (mismas claves que conexion.toml)."""
    claves_net = {f.name for f in fields(NetConfig)}
    claves_serv = {f.name for f in fields(ServiceConfig)} - {"net"}

    def net(base: NetConfig, extra: dict | None) -> NetConfig:
        return replace(base, **{k: v for k, v in (extra or {}).items() if k in claves_net})

    comun = net(NetConfig(), cfg.get("network"))

    def servicio(seccion: dict, defaults: ServiceConfig) -> ServiceConfig:
        s = replace(defaults, **{k: v for k, v in seccion.items() if k in claves_serv})
        return replace(s, net=net(comun, seccion.get("network")))

    llm = servicio(cfg.get("llm", {}), ServiceConfig(base_url="http://localhost:11434/v1",
                                                     model="gemma4", api_key="local"))
    emb = servicio(cfg.get("embeddings", {}), ServiceConfig(model="embeddinggemma"))
    if not emb.base_url:
        emb = replace(emb, base_url=llm.base_url, api_key=emb.api_key or llm.api_key)
    return Conexion(llm=llm, embeddings=emb, fichero=str(cfg.path))
