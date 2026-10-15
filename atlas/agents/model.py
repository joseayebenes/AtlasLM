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

A turn has at most [llm.network] timeout seconds in all, and is asked again when it takes
longer (TURN_RETRIES times). The timeout of the HTTP client is between two reads, and a server
that keeps the connection alive while the model works (OpenRouter sends blanks) never reaches
it: a request of gemma-4-26b hung there for an hour, with the agent waiting.

The reasoning of a turn comes back to ADK as a thought part, and goes back to the model in the
next requests, in the assistant message of its tool calls ([llm] reasoning_field, "reasoning"
by default). The model card of gemma 4 asks for it: the thoughts of the turns with tool calls
stay in the conversation, and its chat template puts them back from `reasoning` or
`reasoning_content`. Without them, the model works out again in every turn what it was doing.

With keep_tool_results (of the agent), the conversation stays short: a read of a file that was
read again later is left out (an old text that the model copies into edit_file), and so is
every large tool result but the last keep_tool_results ones: a note with its short fields
takes its place. JetBrains found this as good as summarizing with the model, at half the cost
(The Complexity Trap, 2025); in a run of atlas develop, the turns of a fix carried 55 000
tokens of input, most of them old reads. The old results go in steps of MASK_STEP, so that the
beginning of the conversation changes seldom (servers reuse the prompt they have seen).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import ConfigDict, PrivateAttr

from atlas.llm import ServiceConfig, create_client
from atlas.llm.connection import reasoning_of
from atlas.llm.structured import repair_json

__all__ = ["AtlasLlm", "reasoning_of", "to_llm_response", "to_messages", "to_tools"]

log = logging.getLogger(__name__)
RETRIES = 2      # times an empty turn cut by max_tokens is asked again, doubling max_tokens
TURN_RETRIES = 2  # times a turn that takes longer than [llm.network] timeout in all is asked again
MASK_STEP = 5     # old tool results are left out in groups of this many
SMALL_RESULT = 600   # characters of a tool result that always stays
SHORT_FIELD = 120    # characters of a field of a left-out result that stays in its note

SCHEMA_KEYS = ("description", "enum", "required", "default", "format", "minimum", "maximum", "title")
FINISH = {"stop": types.FinishReason.STOP, "tool_calls": types.FinishReason.STOP,
          "length": types.FinishReason.MAX_TOKENS}


class AtlasLlm(BaseLlm):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    service: Any = None                       # the ServiceConfig of atlas.llm
    top_p: float | None = None                # sampling of the agent; None = the server default
    top_k: int | None = None                  # not in the OpenAI API: sent in extra_body
    keep_tool_results: int = 0                # large tool results kept whole; 0 = all (see the module)
    _client: Any = PrivateAttr(default=None)

    @classmethod
    def from_service(cls, service: ServiceConfig, client: Any = None, **options) -> "AtlasLlm":
        """The model of `service` ([llm] of atlas.toml, retouched by the agent), with the
        `options` of the agent (top_p, top_k). `client` replaces the OpenAI client, for tests."""
        llm = cls(model=service.model, service=service, **options)
        llm._client = client
        return llm

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client, _ = create_client(self.service)
        return self._client

    async def generate_content_async(self, llm_request: LlmRequest,
                                     stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        kwargs = {"model": self.service.model,
                  "messages": to_messages(llm_request, self.service.reasoning_field, self.keep_tool_results),
                  "temperature": self.service.temperature}
        tools = to_tools(llm_request)
        if tools:
            kwargs["tools"] = tools
        if self.service.max_tokens:
            kwargs["max_tokens"] = self.service.max_tokens
        if self.service.reasoning_effort:
            kwargs["reasoning_effort"] = self.service.reasoning_effort
        if self.top_p is not None:
            kwargs["top_p"] = self.top_p
        extra = {**self.service.extra_body, **({"top_k": self.top_k} if self.top_k is not None else {})}
        if extra:
            kwargs["extra_body"] = extra
        response = await self._create(kwargs)
        for _ in range(RETRIES):
            if not (_empty_and_cut(response) and kwargs.get("max_tokens")):
                break
            log.warning("the model %s spent the whole max_tokens (%d) reasoning and answered nothing: asking again "
                        "with %d", self.service.model, kwargs["max_tokens"], kwargs["max_tokens"] * 2)
            kwargs["max_tokens"] *= 2
            response = await self._create(kwargs)
        yield to_llm_response(response)

    async def _create(self, kwargs: dict) -> Any:
        """One request to the model, at most [llm.network] timeout seconds in all, asked again
        TURN_RETRIES times. The client is sync: it runs in a daemon thread, which a request that
        hangs leaves behind without holding the agent or the end of the process."""
        limit = self.service.net.timeout or None
        for attempt in range(TURN_RETRIES + 1):
            loop, done = asyncio.get_running_loop(), None
            done = loop.create_future()

            def settle(ok: bool, value: Any) -> None:
                if not done.done():
                    done.set_result(value) if ok else done.set_exception(value)

            def work() -> None:
                try:
                    result = (True, self.client.chat.completions.create(**kwargs))
                except BaseException as e:            # to the agent, as if it were raised here
                    result = (False, e)
                try:
                    loop.call_soon_threadsafe(settle, *result)
                except RuntimeError:                  # the loop is gone: nobody waits for it
                    pass

            threading.Thread(target=work, daemon=True, name="atlas-llm-turn").start()
            try:
                return await asyncio.wait_for(done, limit)
            except asyncio.TimeoutError:
                if attempt == TURN_RETRIES:
                    raise TimeoutError(f"the model {self.service.model} did not answer in {limit:g} s, "
                                       f"{TURN_RETRIES + 1} times ([llm.network] timeout)") from None
                log.warning("the model %s did not answer in %g s ([llm.network] timeout): asking again",
                            self.service.model, limit)


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


def to_messages(request: LlmRequest, reasoning_field: str = "reasoning", keep_tool_results: int = 0) -> list[dict]:
    """The conversation of an ADK request as OpenAI chat messages: the system instruction,
    the turns of the user, those of the model (text and tool calls) and the tool results.
    The reasoning of a turn of the model with tool calls goes in its `reasoning_field`
    ("" = it is left out). With keep_tool_results, the old tool results are left out (see
    the module and masked)."""
    messages = []
    system = _text(request.config.system_instruction) if request.config else ""
    if system:
        messages.append({"role": "system", "content": system})
    for content in request.contents:
        texts, calls, results, thoughts = [], [], [], []
        for part in content.parts or []:
            if part.thought:
                if part.text and part.text.strip():
                    thoughts.append(part.text)
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
                                 **({"tool_calls": calls} if calls else {}),
                                 **({reasoning_field: "\n".join(thoughts)} if calls and thoughts and reasoning_field
                                    else {})})
            continue
        for r in results:
            messages.append({"role": "tool", "tool_call_id": r.id or r.name, "_name": r.name, "_response": r.response,
                             "content": json.dumps(r.response, ensure_ascii=False, default=str)})
        if texts:
            messages.append({"role": "user", "content": "\n".join(texts)})
    if keep_tool_results > 0:
        masked(messages, keep_tool_results)
    for m in messages:
        m.pop("_name", None)
        m.pop("_response", None)
    return messages


def masked(messages: list[dict], keep: int) -> None:
    """Leaves out, in place, the tool results that are no longer worth their tokens (see the
    module): a read_file of a file read again later, and the large results before the last
    `keep` ones, in groups of MASK_STEP."""
    tools = [m for m in messages if m["role"] == "tool"]
    last_read: dict[str, int] = {}
    for i, m in enumerate(tools):
        response = m.get("_response")
        if m.get("_name") == "read_file" and isinstance(response, dict) and response.get("file"):
            last_read[response["file"]] = i
    cutoff = max(0, (len(tools) - keep) // MASK_STEP * MASK_STEP)
    for i, m in enumerate(tools):
        response, name = m.get("_response"), m.get("_name") or "a tool"
        if not isinstance(response, dict):
            continue
        if name == "read_file" and response.get("file") and last_read.get(response["file"], i) > i:
            m["content"] = json.dumps({"note": f"an older read of {response['file']} (lines {response.get('start')}-"
                                               f"{response.get('end')}), left out: it was read again later"})
        elif i < cutoff and len(m["content"]) > SMALL_RESULT:
            short = {k: v for k, v in response.items() if isinstance(v, (bool, int, float))
                     or (isinstance(v, str) and len(v) <= SHORT_FIELD)}
            m["content"] = json.dumps({"note": f"the result of {name} of an earlier step, left out to keep the "
                                               "conversation short: call it again if you need it", **short},
                                      ensure_ascii=False, default=str)


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
    """An OpenAI chat completion as an ADK response: its reasoning (a thought part), its text
    and its tool calls."""
    choice = response.choices[0]
    message = choice.message
    parts = []
    reasoning = reasoning_of(message)
    if reasoning:
        parts.append(types.Part(text=reasoning, thought=True))
    if message.content and message.content.strip():
        parts.append(types.Part(text=message.content))
    for call in message.tool_calls or []:
        parts.append(types.Part(function_call=types.FunctionCall(id=call.id, name=call.function.name,
                                                                 args=_arguments(call.function.arguments))))
    usage = getattr(response, "usage", None)
    cost = getattr(usage, "cost", None)            # OpenRouter says what each request cost
    return LlmResponse(
        content=types.Content(role="model", parts=parts or [types.Part(text="")]),
        finish_reason=FINISH.get(choice.finish_reason),
        custom_metadata={"cost": cost} if isinstance(cost, (int, float)) else None,
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=usage.prompt_tokens, candidates_token_count=usage.completion_tokens,
            total_token_count=usage.total_tokens) if usage else None)
