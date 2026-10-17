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

A turn has two limits, in [llm.network]: `idle_timeout`, seconds without a chunk once the
answer has begun, and `turn_timeout`, seconds in all from when the request goes out (the wait
for the limiter does not count), which is also the read timeout of the HTTP client. The wait
for the first chunk is left to turn_timeout: a busy server queues the request without a byte
(Ollama, shared by several processes, sent its first chunk after 458 s and the rest in 3 s),
and cutting it there would only send it to the back of the queue. A thread watches each
request and, when one of them expires, shuts the socket of the response down: the connection
is cut for real, the server sees it go and the slot of max_concurrent is free again. A server
that keeps the connection alive with SSE comments while the model works (OpenRouter) does not
fool it: only chunks count. The turn is then asked again, TURN_RETRIES times. Without
streaming, only turn_timeout applies.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any

from atlas.llm.connection import _TURN, ServiceConfig

__all__ = ["StreamUnsupported", "TURN_RETRIES", "TurnTimeout", "assemble", "complete", "stream_setting", "streamed",
           "streaming"]

TURN_RETRIES = 2      # times a turn cut by idle_timeout or turn_timeout is asked again

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


class TurnTimeout(TimeoutError):
    """A chat turn cut by [llm.network] idle_timeout or turn_timeout, or cancelled."""


def _cut(response: Any) -> None:
    """Shuts the socket of a response down, so that the thread that reads it stops at once and
    the server sees the client go (closing it from another thread would not wake the read)."""
    stream = (getattr(response, "extensions", None) or {}).get("network_stream")
    sock = stream.get_extra_info("socket") if stream is not None else None
    if sock is not None:
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)       # also an SSLSocket, without unwrapping it
        except OSError:
            pass


class _Watch:
    """Watches one request of a turn from another thread (see the module): cuts it when nothing
    comes for `idle` seconds, when it takes more than `total` seconds or when `cancel` is set."""

    def __init__(self, idle: float, total: float, cancel: threading.Event | None):
        self.idle, self.total, self.cancel = idle, total, cancel
        self.lock = threading.Lock()
        self.start = self.last = time.monotonic()
        self.begun = False                                 # a chunk came: from then on, idle counts
        self.response, self.why = None, ""
        self.stopped = threading.Event()
        limits = [x for x in (idle, total) if x]
        if limits or cancel is not None:
            self.step = max(0.01, min([0.5, *(x / 10 for x in limits)]))
            threading.Thread(target=self._run, daemon=True, name="atlas-llm-watch").start()

    def sent(self) -> None:
        """The transport sent the request: the clock starts (again, if it is a retry)."""
        with self.lock:
            self.start = self.last = time.monotonic()

    def tick(self) -> None:
        self.last, self.begun = time.monotonic(), True

    def attach(self, response: Any) -> None:
        with self.lock:
            self.response = response
            late = bool(self.why)
        if late:
            _cut(response)

    def stop(self) -> None:
        self.stopped.set()

    def _expired(self) -> str:
        now = time.monotonic()
        if self.cancel is not None and self.cancel.is_set():
            return "the turn was cancelled"
        if self.total and now - self.start > self.total:
            return f"it took more than {self.total:g} s in all ([llm.network] turn_timeout)"
        if self.idle and self.begun and now - self.last > self.idle:
            return f"nothing came for {self.idle:g} s ([llm.network] idle_timeout)"
        return ""

    def _run(self) -> None:
        while not self.stopped.wait(self.step):
            why = self._expired()
            if why:
                with self.lock:
                    self.why, response = why, self.response
                if response is not None:
                    _cut(response)
                return


def _timed_out(e: BaseException) -> bool:
    """A read or connect timeout of the HTTP client, as it is or wrapped by the OpenAI client."""
    x: BaseException | None = e
    n = 0
    while x is not None and n < 10:
        if "Timeout" in type(x).__name__:
            return True
        x, n = x.__cause__ or x.__context__, n + 1
    return False


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
    content: list = []
    reasoning: dict = {}
    calls: dict = {}
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
                if function is not None and getattr(function, "name", None) \
                        and function.name != "".join(slot["name"]):     # some servers repeat it in every chunk
                    slot["name"].append(function.name)
                if function is not None and getattr(function, "arguments", None):
                    slot["arguments"].append(function.arguments)
    if not seen:
        raise StreamUnsupported("the stream ended without any answer")
    from openai.types.chat import ChatCompletion           # after the chunks: its first import takes a while
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


def _ticking(stream, watch: _Watch | None):
    """The chunks of a stream, telling the watch of each one; once the last has come, there is
    nothing more to wait for."""
    for chunk in stream:
        if watch is not None:
            watch.tick()
        yield chunk
    if watch is not None:
        watch.stop()


def streamed(client: Any, kwargs: dict, watch: _Watch | None = None) -> Any:
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
    if watch is not None and response is not None:
        watch.attach(response)
    kind = response.headers.get("content-type", "") if response is not None else "text/event-stream"
    try:
        if "text/event-stream" not in kind:                # a server that ignored stream
            from openai.types.chat import ChatCompletion
            raise StreamUnsupported(f"it answered {kind or 'something'} instead of an SSE stream",
                                    ChatCompletion.construct(**json.loads(response.read() if response is not None else b"{}")))
        return assemble(_ticking(stream, watch))
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()


def _whole(client: Any, kwargs: dict, watch: _Watch) -> Any:
    """The request without streaming. The OpenAI client gives the response once its headers
    arrive, so that the watch can cut a server that sends blanks while the model works."""
    early = getattr(client.chat.completions, "with_streaming_response", None)
    if early is None:                                      # a client of the tests
        return client.chat.completions.create(**kwargs)
    with early.create(**kwargs) as response:
        watch.attach(response.http_response)
        return response.parse()


def _turn(client: Any, cfg: ServiceConfig, kwargs: dict, stream: bool, cancel: threading.Event | None) -> Any:
    """One request of a turn, watched (see the module); TurnTimeout if it was cut."""
    from openai import Timeout
    net = cfg.net
    read, key = net.turn_timeout or net.timeout, "turn_timeout" if net.turn_timeout else "timeout"
    kwargs = {**kwargs, "timeout": Timeout(net.timeout or None, read=read or None)}
    watch = _Watch(net.idle_timeout if stream else 0, net.turn_timeout, cancel)
    _TURN.watch = watch
    try:
        response = streamed(client, kwargs, watch) if stream else _whole(client, kwargs, watch)
    except StreamUnsupported:
        if watch.why:                                      # it was cut: the stream was not empty by itself
            raise TurnTimeout(watch.why) from None
        raise
    except Exception as e:
        if watch.why:
            raise TurnTimeout(watch.why) from e
        if _timed_out(e):
            raise TurnTimeout(f"nothing came for {read:g} s ([llm.network] {key})") from e
        raise
    finally:
        watch.stop()
        _TURN.watch = None
    if watch.why:                                          # cut where the end of the body looks normal
        raise TurnTimeout(watch.why)
    return response


def _attempt(client: Any, cfg: ServiceConfig, kwargs: dict, cancel: threading.Event | None) -> Any:
    if streaming(cfg):
        try:
            response = _turn(client, cfg, kwargs, True, cancel)
        except StreamUnsupported as e:
            if stream_setting(cfg) and e.response is None:
                raise RuntimeError(f"[llm] stream = true, but the server does not stream: {e}") from e
            learned(cfg, False, str(e))
            if e.response is not None:
                return e.response
        else:
            learned(cfg, True)
            return response
    return _turn(client, cfg, kwargs, False, cancel)


def complete(client: Any, cfg: ServiceConfig, kwargs: dict, cancel: threading.Event | None = None) -> Any:
    """One chat turn with the `kwargs` of the OpenAI client, streamed if the server of cfg
    allows it, cut by idle_timeout or turn_timeout and then asked again TURN_RETRIES times (see
    the module); the answer is a chat completion either way. `cancel` cuts it at once."""
    for attempt in range(TURN_RETRIES + 1):
        try:
            return _attempt(client, cfg, kwargs, cancel)
        except TurnTimeout as e:
            if cancel is not None and cancel.is_set():
                raise
            if attempt == TURN_RETRIES:
                raise TurnTimeout(f"the model {cfg.model} did not answer: {e}, {TURN_RETRIES + 1} times") from None
            log.warning("the model %s did not answer (%s): asking again", cfg.model, e)
