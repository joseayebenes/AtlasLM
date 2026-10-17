"""One chat request, streamed when the server allows it.

    from atlas.llm.chat import complete
    response = complete(client, cfg, {"model": ..., "messages": [...]})

With [llm] stream on, the request goes with `stream=True` and `stream_options={"include_usage":
true}`, and the chunks are put together into the same chat completion the server would have
sent without streaming: the content, the reasoning (`reasoning` or `reasoning_content`), each
tool call by its index (the id and the name come first, the arguments in pieces), the
finish_reason and the usage of the last chunk. Whoever reads the answer cannot tell how it came.

[llm] stream: "" (auto, the default) streams until the server shows it cannot: it rejects the
request naming stream (a 4xx), or answers with a body that is not SSE (it ignored stream), or
closes a stream that brought nothing. Then the request goes again without streaming and
streaming stays off for the rest of the process, for every request to the same `base_url` and
model (like the response_format tiers of JsonLlm). `true` and `false` force it; forced on, a
rejection is an error. `atlas probe` says whether the server streams.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from atlas.llm.connection import ServiceConfig

__all__ = ["StreamUnsupported", "assemble", "complete", "stream_setting", "streamed", "streaming"]

log = logging.getLogger(__name__)

# Whether each server streams, learned by (base_url, model) for the whole process: False once it
# showed it cannot
_STREAMS: dict[tuple[str, str], bool] = {}
_STREAMS_LOCK = threading.Lock()


class StreamUnsupported(Exception):
    """The server cannot stream: it rejected stream, ignored it or sent an empty stream. If it
    ignored it, `response` is the answer it gave without streaming."""

    def __init__(self, msg: str, response: Any = None):
        super().__init__(msg)
        self.response = response


def stream_setting(cfg: ServiceConfig) -> bool | None:
    """[llm] stream: True or False when forced, None for auto ("" or "auto")."""
    value = str(cfg.stream).strip().lower()
    if value in ("", "auto"):
        return None
    return value in ("1", "true", "yes", "on")


def streaming(cfg: ServiceConfig) -> bool:
    """Whether the requests to the server of cfg go streamed, as far as this process knows."""
    forced = stream_setting(cfg)
    if forced is not None:
        return forced
    with _STREAMS_LOCK:
        return _STREAMS.get((cfg.base_url, cfg.model), True)


def learned(cfg: ServiceConfig, ok: bool, why: str = "", say: bool = True) -> None:
    """Notes whether the server of cfg streams (atlas probe, or a request that showed it); a
    server that cannot is said once (unless `say` is False: probe says it its own way)."""
    key = (cfg.base_url, cfg.model)
    with _STREAMS_LOCK:
        before = _STREAMS.get(key)
        _STREAMS[key] = ok
    if say and not ok and before is not False and stream_setting(cfg) is None:
        log.warning("the server does not stream (%s): going on without streaming", why)


def _rejects_stream(e: Exception) -> bool:
    """A 4xx that names stream (or stream_options) in what the server says."""
    status = getattr(e, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status not in (401, 403, 404, 409, 429) \
        and "stream" in f"{getattr(e, 'message', '')} {getattr(e, 'body', '')} {e}".lower()


def _field(obj: Any, name: str) -> Any:
    """A field of a chunk of the OpenAI client, also one it does not know (in model_extra)."""
    value = getattr(obj, name, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(name)
    return value


def assemble(chunks) -> Any:
    """The chat completion that the chunks of a stream make up (see the module). Raises
    StreamUnsupported if the stream brought no choice at all."""
    from openai.types.chat import ChatCompletion
    content, reasoning, calls = [], {}, {}
    head: dict = {}
    finish, usage, seen = None, None, False
    for chunk in chunks:
        if not head and getattr(chunk, "id", None) is not None:
            head = {"id": chunk.id, "created": getattr(chunk, "created", 0) or 0,
                    "model": getattr(chunk, "model", "") or ""}
        if _field(chunk, "usage") is not None:
            usage = _field(chunk, "usage")
        for choice in getattr(chunk, "choices", None) or []:
            seen = True
            delta = getattr(choice, "delta", None)
            if getattr(choice, "finish_reason", None):
                finish = choice.finish_reason
            if delta is None:
                continue
            if getattr(delta, "content", None):
                content.append(delta.content)
            for name in ("reasoning", "reasoning_content"):
                text = _field(delta, name)
                if isinstance(text, str) and text:
                    reasoning.setdefault(name, []).append(text)
            for call in getattr(delta, "tool_calls", None) or []:
                index = getattr(call, "index", None)
                index = len(calls) if index is None else index
                slot = calls.setdefault(index, {"id": None, "type": "function", "name": [], "arguments": []})
                if getattr(call, "id", None):
                    slot["id"] = call.id
                function = getattr(call, "function", None)
                if function is not None and getattr(function, "name", None):
                    slot["name"].append(function.name)
                if function is not None and getattr(function, "arguments", None):
                    slot["arguments"].append(function.arguments)
    if not seen:
        raise StreamUnsupported("the stream ended without any answer")
    message: dict = {"role": "assistant", "content": "".join(content) if content or not calls else None}
    message.update({name: "".join(parts) for name, parts in reasoning.items()})
    if calls:
        message["tool_calls"] = [{"id": slot["id"] or f"call_{index}", "type": "function",
                                  "function": {"name": "".join(slot["name"]), "arguments": "".join(slot["arguments"])}}
                                 for index, slot in sorted(calls.items())]
    data = {"id": head.get("id", ""), "object": "chat.completion", "created": head.get("created", 0),
            "model": head.get("model", ""),
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish or ("tool_calls" if calls else "stop")}]}
    if usage is not None:
        data["usage"] = usage.model_dump() if hasattr(usage, "model_dump") else vars(usage)
    return ChatCompletion.construct(**data)


def streamed(client: Any, kwargs: dict) -> Any:
    """The request with stream on, put together (see the module), whatever [llm] stream says.
    StreamUnsupported if the server rejected stream, ignored it (with the answer it gave) or
    sent an empty stream."""
    try:
        stream = client.chat.completions.create(**kwargs, stream=True, stream_options={"include_usage": True})
    except Exception as e:
        if _rejects_stream(e):
            raise StreamUnsupported(f"it rejects stream: {e}") from e
        raise
    if getattr(stream, "choices", None) is not None:      # a client that ignored stream
        raise StreamUnsupported("it answered without streaming", stream)
    response = getattr(stream, "response", None)
    kind = response.headers.get("content-type", "") if response is not None else "text/event-stream"
    try:
        if "text/event-stream" not in kind:                # a server that ignored stream
            from openai.types.chat import ChatCompletion
            raise StreamUnsupported(f"it answered {kind or 'something'} instead of an SSE stream",
                                    ChatCompletion.construct(**json.loads(response.read())))
        return assemble(stream)
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()


def complete(client: Any, cfg: ServiceConfig, kwargs: dict) -> Any:
    """One chat request with the `kwargs` of the OpenAI client, streamed if the server of cfg
    allows it (see the module); the answer is a chat completion either way."""
    if not streaming(cfg):
        return client.chat.completions.create(**kwargs)
    try:
        response = streamed(client, kwargs)
    except StreamUnsupported as e:
        if stream_setting(cfg) and e.response is None:
            raise RuntimeError(f"[llm] stream = true, but the server does not stream: {e}") from e
        learned(cfg, False, str(e))
        return e.response if e.response is not None else client.chat.completions.create(**kwargs)
    learned(cfg, True)
    return response
