"""AtlasLlm: an ADK model over the OpenAI-compatible client of atlas.llm, so that the agents
go through the same proxy, TLS, request limit and retries as the rest of atlas.

ADK speaks in google.genai types (Content, Part, FunctionCall, FunctionDeclaration); the
servers atlas talks to (Ollama, vLLM, LM Studio, a gateway) speak the OpenAI chat API. This
module translates one into the other: the conversation with its tool calls and results, and
the tools with their JSON schema. One request per model turn, without streaming.

A reasoning model can spend the whole max_tokens thinking and answer nothing (finish_reason
"length", no text, no tool call); ADK would take that empty turn as the final answer and end
the agent in silence. That turn is asked again with twice the max_tokens, at most RETRIES
times, with a warning.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import ConfigDict, PrivateAttr

from atlas.llm import ServiceConfig, create_client
from atlas.llm.structured import repair_json

__all__ = ["AtlasLlm", "to_llm_response", "to_messages", "to_tools"]

log = logging.getLogger(__name__)
RETRIES = 2      # times an empty turn cut by max_tokens is asked again, doubling max_tokens

SCHEMA_KEYS = ("description", "enum", "required", "default", "format", "minimum", "maximum", "title")
FINISH = {"stop": types.FinishReason.STOP, "tool_calls": types.FinishReason.STOP,
          "length": types.FinishReason.MAX_TOKENS}


class AtlasLlm(BaseLlm):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    service: Any = None                       # the ServiceConfig of atlas.llm
    _client: Any = PrivateAttr(default=None)

    @classmethod
    def from_service(cls, service: ServiceConfig, client: Any = None) -> "AtlasLlm":
        """The model of `service` ([llm] of atlas.toml, retouched by the agent). `client`
        replaces the OpenAI client, for tests."""
        llm = cls(model=service.model, service=service)
        llm._client = client
        return llm

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client, _ = create_client(self.service)
        return self._client

    async def generate_content_async(self, llm_request: LlmRequest,
                                     stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        kwargs = {"model": self.service.model, "messages": to_messages(llm_request),
                  "temperature": self.service.temperature}
        tools = to_tools(llm_request)
        if tools:
            kwargs["tools"] = tools
        if self.service.max_tokens:
            kwargs["max_tokens"] = self.service.max_tokens
        if self.service.reasoning_effort:
            kwargs["reasoning_effort"] = self.service.reasoning_effort
        if self.service.extra_body:
            kwargs["extra_body"] = self.service.extra_body
        response = await asyncio.to_thread(self.client.chat.completions.create, **kwargs)   # the client is sync
        for _ in range(RETRIES):
            if not (_empty_and_cut(response) and kwargs.get("max_tokens")):
                break
            log.warning("the model %s spent the whole max_tokens (%d) reasoning and answered nothing: asking again "
                        "with %d", self.service.model, kwargs["max_tokens"], kwargs["max_tokens"] * 2)
            kwargs["max_tokens"] *= 2
            response = await asyncio.to_thread(self.client.chat.completions.create, **kwargs)
        yield to_llm_response(response)


def _empty_and_cut(response: Any) -> bool:
    """Cut by max_tokens with no text and no tool call: all of it went to reasoning."""
    choice = response.choices[0]
    return choice.finish_reason == "length" and not (choice.message.content or "").strip() \
        and not choice.message.tool_calls


def _text(value: Any) -> str:
    """The text of a system instruction: a string, a Content or a list of them."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n\n".join(_text(v) for v in value)
    parts = getattr(value, "parts", None)
    if parts is not None:
        return "\n".join(p.text for p in parts if getattr(p, "text", None))
    return str(getattr(value, "text", "") or "")


def to_messages(request: LlmRequest) -> list[dict]:
    """The conversation of an ADK request as OpenAI chat messages: the system instruction,
    the turns of the user, those of the model (text and tool calls) and the tool results."""
    messages = []
    system = _text(request.config.system_instruction) if request.config else ""
    if system:
        messages.append({"role": "system", "content": system})
    for content in request.contents:
        texts, calls, results = [], [], []
        for part in content.parts or []:
            if part.thought:
                continue
            if part.function_call:
                fc = part.function_call
                arguments = json.dumps(fc.args or {}, ensure_ascii=False)
                calls.append({"id": fc.id or fc.name, "type": "function",
                              "function": {"name": fc.name, "arguments": arguments}})
            elif part.function_response:
                results.append(part.function_response)
            elif part.text:
                texts.append(part.text)
        if content.role == "model":
            if texts or calls:
                messages.append({"role": "assistant", "content": "\n".join(texts) or None,
                                 **({"tool_calls": calls} if calls else {})})
            continue
        for r in results:
            messages.append({"role": "tool", "tool_call_id": r.id or r.name,
                             "content": json.dumps(r.response, ensure_ascii=False, default=str)})
        if texts:
            messages.append({"role": "user", "content": "\n".join(texts)})
    return messages


def _json_schema(schema: dict) -> dict:
    """A google.genai Schema (as a dict, with types in upper case) as a JSON schema."""
    out: dict = {}
    for key, value in schema.items():
        if key == "type":
            out["type"] = str(getattr(value, "value", value)).lower()
        elif key == "properties":
            out["properties"] = {name: _json_schema(s) for name, s in value.items()}
        elif key == "items":
            out["items"] = _json_schema(value)
        elif key == "any_of":
            out["anyOf"] = [_json_schema(s) for s in value]
        elif key in SCHEMA_KEYS:
            out[key] = value
    return out


def to_tools(request: LlmRequest) -> list[dict]:
    """The function declarations of an ADK request as OpenAI tools."""
    out = []
    for tool in (request.config.tools or []) if request.config else []:
        for fd in getattr(tool, "function_declarations", None) or []:
            if fd.parameters_json_schema:
                parameters = dict(fd.parameters_json_schema)
            elif fd.parameters:
                parameters = _json_schema(fd.parameters.model_dump(exclude_none=True))
            else:
                parameters = {"type": "object", "properties": {}}
            out.append({"type": "function", "function": {"name": fd.name, "description": fd.description or "",
                                                         "parameters": parameters}})
    return out


def _arguments(text: str | None) -> dict:
    """The arguments of a tool call; small models sometimes leave out the last brace, or
    cut or garble the JSON."""
    for candidate in (text or "{}", (text or "") + "}"):
        try:
            args = json.loads(candidate)
            break
        except ValueError:
            continue
    else:
        args = repair_json(text or "") or {}
    return args if isinstance(args, dict) else {}


def to_llm_response(response: Any) -> LlmResponse:
    """An OpenAI chat completion as an ADK response: its text and its tool calls."""
    choice = response.choices[0]
    message = choice.message
    parts = []
    if message.content and message.content.strip():
        parts.append(types.Part(text=message.content))
    for call in message.tool_calls or []:
        parts.append(types.Part(function_call=types.FunctionCall(id=call.id, name=call.function.name,
                                                                 args=_arguments(call.function.arguments))))
    usage = getattr(response, "usage", None)
    return LlmResponse(
        content=types.Content(role="model", parts=parts or [types.Part(text="")]),
        finish_reason=FINISH.get(choice.finish_reason),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=usage.prompt_tokens, candidates_token_count=usage.completion_tokens,
            total_token_count=usage.total_tokens) if usage else None)
