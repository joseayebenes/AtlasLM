"""Structured answers: ask the chat model for one JSON object and get a dict back.

Comes from the `LLM` class and the JSON helpers of legacy/pdf_struct.py. Small models
often wrap the JSON in prose or a ```json block, cut it when they reach max_tokens or
leave trailing commas, so the answer goes through three stages:

1. `parse_json`: the largest JSON object in the text that has one of the expected keys.
2. `repair_json`: what can be saved from a cut or slightly broken object.
3. If neither works, the model is asked once to correct itself, showing it the error.

An answer cut by length is never taken as good: `TruncatedResponse` carries what could be
repaired so that the caller can split its input and ask again.

    from atlas.llm import JsonLlm
    llm = JsonLlm(conn.llm)
    d = llm.ask_json(system, user, schema, "overview")
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
from typing import Callable

from atlas.llm.connection import ServiceConfig, create_client

__all__ = ["JsonLlm", "TruncatedResponse", "parse_json", "repair_json"]

log = logging.getLogger(__name__)


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


class JsonLlm:
    """Chat client that answers with JSON objects (`ask_json`) or with text (`ask_text`).
    It asks for `response_format` with a JSON schema and, if the server does not accept
    it, goes on without it. Thread-safe: one instance can be shared by the workers that
    analyse sections in parallel, and `concurrency` (0 = no limit) caps how many requests
    of all of them are in flight at once (e.g. the sections of several documents that are
    processed at the same time); None takes [llm] concurrency."""

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
        self.schema_ok = True   # turned off if the server does not accept json_schema
        self._lock = threading.Lock()
        concurrency = cfg.concurrency if concurrency is None else concurrency
        self.concurrency = concurrency
        self._slots = threading.BoundedSemaphore(concurrency) if concurrency > 0 else contextlib.nullcontext()

    def _create(self, **kwargs):
        with self._slots:
            return self.client.chat.completions.create(**kwargs)

    def _kwargs(self, messages: list[dict]) -> dict:
        kwargs = dict(model=self.model, temperature=self.temperature, messages=messages)
        if self.cfg.max_tokens:
            kwargs["max_tokens"] = self.cfg.max_tokens
        if self.cfg.reasoning_effort:
            kwargs["reasoning_effort"] = self.cfg.reasoning_effort
        if self.cfg.extra_body:
            kwargs["extra_body"] = self.cfg.extra_body
        return kwargs

    def ask_text(self, system: str, user: str) -> str:
        """A plain text answer (e.g. a cited answer for a person)."""
        resp = self._create(**self._kwargs(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]))
        return (resp.choices[0].message.content or "").strip()

    def _call(self, messages: list[dict], schema: dict, name: str) -> tuple[str, str]:
        while True:
            kwargs = self._kwargs(messages)
            if self.schema_ok:
                kwargs["response_format"] = {"type": "json_schema", "json_schema":
                                             {"name": name, "schema": schema}}
            try:
                resp = self._create(**kwargs)
            except self._status_error as e:
                if "response_format" in kwargs and e.status_code in self.NO_SCHEMA_STATUSES:
                    with self._lock:
                        if self.schema_ok:
                            log.warning("the server does not accept json_schema, going on without it: %s", e)
                            self.schema_ok = False
                    continue
                raise
            ch = resp.choices[0]
            return ch.message.content or "", (ch.finish_reason or "")

    def ask_json(self, system: str, user: str, schema: dict, name: str,
                 on_failure: Callable[[int, str, str], None] | None = None) -> dict:
        """Asks for a JSON object. If the answer is not valid: repairs it if it is a detail,
        raises TruncatedResponse if it was cut by length (so the input can be split), and
        otherwise asks the model for a correction showing it its error (instead of
        repeating the same request). `on_failure(attempt, text, finish_reason)` is called
        with every invalid answer, to keep it for diagnosis."""
        keys = schema.get("required") or list(schema.get("properties", {}))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        text, finish = self._call(messages, schema, name)
        for attempt in (1, 2):
            if finish != "length":                 # a cut answer is never taken as good
                try:
                    return parse_json(text, keys)
                except ValueError as e:
                    error = e
            else:
                error = ValueError("answer cut by length")
            if on_failure:
                on_failure(attempt, text, finish)
            repaired = repair_json(text, keys)
            if finish == "length":
                raise TruncatedResponse(f"answer cut by length ({len(text)} chars)", repaired)
            if repaired is not None:
                return repaired
            if attempt == 2:
                break
            messages = messages + [
                {"role": "assistant", "content": text[:3000]},
                {"role": "user", "content": f"That was not a valid JSON object ({error}). Reply "
                                            f"again with ONLY the JSON object, nothing before or after it."}]
            text, finish = self._call(messages, schema, name)
        raise RuntimeError(f"the model did not return valid JSON, not even after asking for a correction: {error}")
