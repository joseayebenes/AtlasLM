"""Structured answers: ask the chat model for one JSON object and get a dict back.

Small models often wrap the JSON in prose or a ```json block, cut it when they reach max_tokens or
leave trailing commas, so the answer goes through three stages:

1. `parse_json`: the largest JSON object in the text that has one of the expected keys.
2. `repair_json`: what can be saved from a cut or slightly broken object.
3. `validate`: the object against the schema (the subset atlas uses: type, properties,
   required, items, enum).
4. If there is no object, or it breaks the schema, the model is asked once to correct
   itself, quoting the error (`requirements[2].text must be a string`).

An answer cut by length is never taken as good: `TruncatedResponse` carries what could be
repaired so that the caller can split its input and ask again.

The answer is asked for with `response_format` in three tiers: `json_schema`, `json_object`
(most servers that reject a schema accept it, and it already keeps prose and code blocks
out) and none. A server that rejects a tier is asked with the next one from then on, by
every JsonLlm of the process that goes to the same `base_url` and model
(`structured_output`).

    from atlas.llm import JsonLlm
    llm = JsonLlm(conn.llm)
    d = llm.ask_json(system, user, schema, "overview")
    d = llm.ask_json(system, user, schema, "overview", model="other", reasoning_effort="low")
    small = llm.using("other", "low")       # the same for every request, on the same connection
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
from typing import Callable

from atlas.llm.chat import complete
from atlas.llm.connection import ServiceConfig, create_client

__all__ = ["JsonLlm", "TextAnswer", "TruncatedResponse", "parse_json", "repair_json", "structured_output",
           "using", "validate"]

log = logging.getLogger(__name__)

# How the JSON is asked for, from the best to none
TIERS = ("json_schema", "json_object", "none")

# The tier each server accepts, learned by (base_url, model) for the whole process: a JsonLlm
# is created per command and per MCP call, and each would pay another rejected request.
_TIERS: dict[tuple[str, str], str] = {}
_TIERS_LOCK = threading.Lock()


def structured_output(cfg: ServiceConfig) -> str:
    """The `response_format` tier that the server of cfg accepts, as far as this process
    knows: "json_schema" (until one is rejected), "json_object" or "none"."""
    with _TIERS_LOCK:
        return _TIERS.get((cfg.base_url, cfg.model), TIERS[0])


def _step_down(cfg: ServiceConfig, rejected: str, error: Exception) -> None:
    """The server rejected `rejected`: from now on, the next tier (once, even if several
    threads were rejected at the same time)."""
    key = (cfg.base_url, cfg.model)
    with _TIERS_LOCK:
        if _TIERS.get(key, TIERS[0]) != rejected:
            return
        _TIERS[key] = TIERS[TIERS.index(rejected) + 1]
    log.warning("the server does not accept response_format %s, going on with %s: %s",
                rejected, _TIERS[key], error)


_TYPES: dict[str, type | tuple[type, ...]] = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list,
          "object": dict, "null": type(None)}
_NAMES = {"string": "a string", "integer": "an integer", "number": "a number", "boolean": "true or false",
          "array": "a list", "object": "an object", "null": "null"}


def _is(value, type_name: str) -> bool:
    if type_name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES.get(type_name, object))


def validate(obj, schema: dict, path: str = "") -> list[str]:
    """What `obj` breaks of a JSON schema, as sentences that name the place
    ("requirements[2].text must be a string"); [] if nothing. Only the subset atlas uses:
    type, properties, required, items and enum."""
    where = path or "the answer"
    types = schema.get("type")
    types = [types] if isinstance(types, str) else types or []
    if types and not any(_is(obj, t) for t in types):
        return [f"{where} must be {' or '.join(_NAMES.get(t, t) for t in types)}"]
    problems = []
    if "enum" in schema and obj not in schema["enum"]:
        problems.append(f"{where} must be one of {', '.join(json.dumps(v) for v in schema['enum'])}")
    if isinstance(obj, dict):
        prefix = f"{path}." if path else ""
        problems += [f"{prefix}{k} is missing" for k in schema.get("required", []) if k not in obj]
        for k, sub in schema.get("properties", {}).items():
            if k in obj and isinstance(sub, dict):
                problems += validate(obj[k], sub, prefix + k)
    if isinstance(obj, list) and isinstance(schema.get("items"), dict):
        for i, x in enumerate(obj):
            problems += validate(x, schema["items"], f"{path}[{i}]")
    return problems


def _is_answer(obj, keys: list[str] | None) -> bool:
    """Avoids taking an inner object of a broken JSON (e.g. a single requirement) as the
    answer: it must have at least one of the expected keys."""
    return isinstance(obj, dict) and (not keys or any(k in obj for k in keys))


def parse_json(text: str, keys: list[str] | None = None) -> dict:
    """The largest JSON object in the answer with one of the expected keys, even if it is
    surrounded by text, inside a ```json``` block or preceded by reasoning with braces."""
    t = re.sub(r"```(?:json)?", "", text or "")
    dec = json.JSONDecoder()
    best, best_len, i = None, -1, 0
    while (i := t.find("{", i)) >= 0:
        try:
            obj, end = dec.raw_decode(t, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if _is_answer(obj, keys) and end - i > best_len:
            best, best_len = obj, end - i
            i = end
        else:
            i += 1
    if best is None:
        raise ValueError("the answer does not contain a valid JSON object")
    return best


def repair_json(text: str, keys: list[str] | None = None) -> dict | None:
    """Recovers what can be used from a cut JSON (output truncated by length) or one with
    trailing commas: cuts at the last complete element and closes what is still open."""
    t = re.sub(r"```(?:json)?", "", text or "")
    start = t.find("{")
    if start < 0:
        return None
    t = t[start:]
    stack: list[str] = []
    cuts: list[tuple[int, list[str]]] = []
    in_string = escape = False
    for i, c in enumerate(t):
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c in "{[":
            stack.append(c)
        elif c in "}]":
            if stack:
                stack.pop()
            if not stack:
                cuts.append((i + 1, []))
                break
            cuts.append((i + 1, list(stack)))
        elif c == ",":
            cuts.append((i, list(stack)))
    for pos, still_open in reversed(cuts[-200:]):
        candidate = t[:pos].rstrip().rstrip(",") + "".join("}" if x == "{" else "]" for x in reversed(still_open))
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if _is_answer(obj, keys):
            return obj
    return None


class TruncatedResponse(Exception):
    """The model output was cut by length. `partial` is what could be recovered, or None."""

    def __init__(self, msg: str, partial: dict | None):
        super().__init__(msg)
        self.partial = partial


class TextAnswer(str):
    """The text of `ask_text`, with how it came: `truncated` if it was still cut by
    max_tokens after asking again with twice the budget, and how many `requests` it took."""
    truncated: bool = False
    requests: int = 1

    def __new__(cls, text: str, truncated: bool = False, requests: int = 1):
        obj = super().__new__(cls, text)
        obj.truncated, obj.requests = truncated, requests
        return obj


class JsonLlm:
    """Chat client that answers with JSON objects (`ask_json`) or with text (`ask_text`).
    It asks for `response_format` in tiers (see the module) and learns which one the server
    accepts. Thread-safe: one instance can be shared by the workers that
    analyse sections in parallel, and `concurrency` (0 = no limit) caps how many requests
    of all of them are in flight at once (e.g. the sections of several documents that are
    processed at the same time); None takes [llm] concurrency. Each request can take another
    model or reasoning effort than those of `cfg` (`model`, `reasoning_effort`), on the same
    connection, rate limiter and slots: so do the different tasks of a run (analysing
    documents, rewriting questions, answering)."""

    # How servers say they do not support response_format: 400 (most), 422 (some vLLM
    # versions), 501 "structured output is unavailable" (Ollama with some models)
    NO_SCHEMA_STATUSES = (400, 422, 501)

    def __init__(self, cfg: ServiceConfig, concurrency: int | None = None):
        from openai import APIStatusError
        self.cfg = cfg
        self.client, self.proxy = create_client(cfg)
        self._status_error = APIStatusError
        self.model = cfg.model
        self.temperature = cfg.temperature
        concurrency = cfg.concurrency if concurrency is None else concurrency
        self.concurrency = concurrency
        self._slots = threading.BoundedSemaphore(concurrency) if concurrency > 0 else contextlib.nullcontext()

    def _create(self, **kwargs):
        with self._slots:
            return complete(self.client, self.cfg, kwargs)

    def _kwargs(self, messages: list[dict], model: str | None = None, reasoning_effort: str | None = None) -> dict:
        """The arguments of a request: those of cfg, with `model` and `reasoning_effort`
        instead of its own when they are given (None or "": those of cfg)."""
        kwargs = dict(model=model or self.model, temperature=self.temperature, messages=messages)
        if self.cfg.max_tokens:
            kwargs["max_tokens"] = self.cfg.max_tokens
        if reasoning_effort or self.cfg.reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort or self.cfg.reasoning_effort
        if self.cfg.extra_body:
            kwargs["extra_body"] = self.cfg.extra_body
        return kwargs

    @property
    def structured(self) -> str:
        """The response_format tier used with this server: json_schema, json_object or none."""
        return structured_output(self.cfg)

    def using(self, model: str = "", reasoning_effort: str = "") -> "JsonLlm":
        """This client with another model or reasoning effort in every request ("": that of
        cfg), for code that does not pass them request by request (the analysis of the
        documents of a source)."""
        return using(self, model, reasoning_effort)

    def ask_text(self, system: str, user: str, model: str | None = None,
                 reasoning_effort: str | None = None) -> TextAnswer:
        """A plain text answer (e.g. a cited answer for a person). If it comes empty or cut
        by max_tokens, it is asked once more with twice the budget (twice the tokens the
        first one spent if no max_tokens is set); if that one is cut too, the answer says
        `truncated`. `model` and `reasoning_effort` replace those of cfg in this request."""
        kwargs = self._kwargs([{"role": "system", "content": system}, {"role": "user", "content": user}],
                              model, reasoning_effort)
        resp = self._create(**kwargs)
        ch = resp.choices[0]
        text, finish = (ch.message.content or "").strip(), ch.finish_reason or ""
        if text and finish != "length":
            return TextAnswer(text)
        budget = kwargs.get("max_tokens") or getattr(getattr(resp, "usage", None), "completion_tokens", 0) or 0
        if budget:
            kwargs["max_tokens"] = 2 * budget
        log.warning("the answer came %s (finish_reason %s): asking again with max_tokens %s",
                    "cut" if text else "empty", finish or "-", kwargs.get("max_tokens", "unset"))
        resp = self._create(**kwargs)
        ch = resp.choices[0]
        again, finish = (ch.message.content or "").strip(), ch.finish_reason or ""
        if again or not text:
            text = again
        else:
            finish = "length"                    # the first one, cut, is all there is
        if finish == "length":
            log.warning("the answer is still cut by max_tokens (%s)", kwargs.get("max_tokens", "unset"))
        return TextAnswer(text, truncated=finish == "length", requests=2)

    def _call(self, messages: list[dict], schema: dict, name: str, model: str | None = None,
              reasoning_effort: str | None = None) -> tuple[str, str]:
        while True:
            kwargs = self._kwargs(messages, model, reasoning_effort)
            tier = self.structured
            if tier == "json_schema":
                kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}
            elif tier == "json_object":
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = self._create(**kwargs)
            except self._status_error as e:
                if tier != "none" and e.status_code in self.NO_SCHEMA_STATUSES:
                    _step_down(self.cfg, tier, e)
                    continue
                raise
            ch = resp.choices[0]
            return ch.message.content or "", (ch.finish_reason or "")

    def probe_structured(self) -> str:
        """One small JSON request, to learn the tier the server accepts (atlas check)."""
        self._call([{"role": "user", "content": 'Answer with the JSON object {"ok": true}.'}],
                   {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}, "check")
        return self.structured

    def ask_json(self, system: str, user: str, schema: dict, name: str,
                 on_failure: Callable[[int, str, str], None] | None = None, model: str | None = None,
                 reasoning_effort: str | None = None) -> dict:
        """Asks for a JSON object. If the answer is not valid: repairs it if it is a detail,
        raises TruncatedResponse if it was cut by length (so the input can be split), and
        otherwise asks the model for a correction quoting its first error (instead of
        repeating the same request). An object that still breaks the schema after the
        correction is returned as it is (the correction only if it has fewer errors): the callers make it
        fit. `on_failure(attempt, text, finish_reason)` is called with every answer that is
        thrown away, to keep it for diagnosis. `model` and `reasoning_effort` replace those of
        cfg in this request (see _kwargs)."""
        keys = schema.get("required") or list(schema.get("properties", {}))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        text, finish = self._call(messages, schema, name, model, reasoning_effort)
        best: tuple[int, dict] | None = None             # (errors, object) of the best answer so far
        for attempt in (1, 2):
            if finish == "length":                        # a cut answer is never taken as good
                if on_failure:
                    on_failure(attempt, text, finish)
                repaired = repair_json(text, keys)
                raise TruncatedResponse(f"answer cut by length ({len(text)} chars)", repaired)
            try:
                obj: dict | None = parse_json(text, keys)
                error = ""
            except ValueError as e:
                obj, error = repair_json(text, keys), f"That was not a valid JSON object ({e})"
            if obj is not None:
                problems = validate(obj, schema)
                if not problems:
                    return obj
                if best is None or len(problems) < best[0]:
                    best = (len(problems), obj)
                    if attempt == 2:
                        break
                error = error or (f"That JSON object does not follow the schema: {problems[0]}"
                                  + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""))
            if on_failure:
                on_failure(attempt, text, finish)
            if attempt == 2:
                break
            messages = messages + [
                {"role": "assistant", "content": text[:3000]},
                {"role": "user", "content": f"{error}. Reply again with ONLY the JSON object, nothing before "
                                            f"or after it."}]
            text, finish = self._call(messages, schema, name, model, reasoning_effort)
        if best is not None:
            log.warning("the model answered a JSON object that does not follow the schema %s: %s",
                        name, validate(best[1], schema)[0])
            return best[1]
        raise RuntimeError(f"the model did not return valid JSON, not even after asking for a correction: {error}")


class _Using:
    """A JsonLlm that asks with its own model and reasoning effort; everything else (the
    connection, the limiter, the slots, `schema_ok`) is that of the JsonLlm it wraps."""

    def __init__(self, llm: JsonLlm, model: str, reasoning_effort: str):
        self._llm = llm
        self.model = model or llm.model
        self.reasoning_effort = reasoning_effort or llm.cfg.reasoning_effort

    def ask_json(self, system: str, user: str, schema: dict, name: str,
                 on_failure: Callable[[int, str, str], None] | None = None, model: str | None = None,
                 reasoning_effort: str | None = None) -> dict:
        return self._llm.ask_json(system, user, schema, name, on_failure, model or self.model,
                                  reasoning_effort or self.reasoning_effort)

    def ask_text(self, system: str, user: str, model: str | None = None, reasoning_effort: str | None = None) -> str:
        return self._llm.ask_text(system, user, model or self.model, reasoning_effort or self.reasoning_effort)

    def using(self, model: str = "", reasoning_effort: str = "") -> JsonLlm:
        return using(self._llm, model or self.model, reasoning_effort or self.reasoning_effort)

    def __getattr__(self, name: str):
        return getattr(self._llm, name)


def using(llm: JsonLlm, model: str = "", reasoning_effort: str = "") -> JsonLlm:
    """`llm` with another model or reasoning effort in every request ("": its own); `llm`
    itself if neither is given."""
    if not model and not reasoning_effort:
        return llm
    return _Using(llm, model, reasoning_effort)  # type: ignore[return-value]
