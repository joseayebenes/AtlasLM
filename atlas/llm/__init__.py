"""Connection to the LLM and embeddings APIs (proxy, TLS, limits, retries).

`connection.py` is legacy/conexion.py translated to English. In atlas the configuration
comes from the [llm], [embeddings] and [network] sections of atlas.toml via `from_config`.
`structured.py` asks the chat model for JSON objects (in the response_format tier the server
accepts), checks them against their schema and repairs broken answers.
"""
from __future__ import annotations

from dataclasses import fields, replace

from atlas.llm.connection import (  # noqa: F401
    Connection, NetConfig, ServiceConfig, check, check_service, create_client, describe, diagnose,
    effective_proxy, is_connection_error, mask_credentials, note_usage, rate_limiter_for, set_usage_label, show, usage,
    usage_label, usage_since,
)
from atlas.llm.structured import (  # noqa: F401
    JsonLlm, TextAnswer, TruncatedResponse, parse_json, repair_json, structured_output, validate,
)


def model_failed(e: BaseException, cfg=None) -> dict:
    """What a tool answers when the model server fails: the diagnosis and, if the
    configuration reads variables this process lacks (the proxy, the key), which ones."""
    from atlas.config import unset_hint
    hint = unset_hint(cfg) if cfg is not None else ""
    return {"error": f"the model server failed: {diagnose(e)}" + (f". {hint}" if hint else "")}


def from_config(cfg) -> Connection:
    """Builds the Connection from atlas.toml (same keys as connection.toml)."""
    net_keys = {f.name for f in fields(NetConfig)}
    service_keys = {f.name for f in fields(ServiceConfig)} - {"net"}

    def net(base: NetConfig, extra: dict | None) -> NetConfig:
        return replace(base, **{k: v for k, v in (extra or {}).items() if k in net_keys})

    common = net(NetConfig(), cfg.get("network"))

    def service(section: dict, defaults: ServiceConfig) -> ServiceConfig:
        s = replace(defaults, **{k: v for k, v in section.items() if k in service_keys})
        return replace(s, net=net(common, section.get("network")))

    llm = service(cfg.get("llm", {}), ServiceConfig(base_url="http://localhost:11434/v1",
                                                    model="gemma4", api_key="local"))
    emb = service(cfg.get("embeddings", {}), ServiceConfig(model="embeddinggemma"))
    if not emb.base_url:
        emb = replace(emb, base_url=llm.base_url, api_key=emb.api_key or llm.api_key)
    return Connection(llm=llm, embeddings=emb, file=str(cfg.path))
