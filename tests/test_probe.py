"""atlas probe: the chat model as an agent, with a stub of the OpenAI client."""
import json
from types import SimpleNamespace

from atlas.llm import ServiceConfig
from atlas.llm.probe import probe


class Server:
    """Answers the probe: a tool call (with reasoning) and then the answer. `renders` says
    whether its chat template puts back the reasoning of the assistant message (it shows in
    prompt_tokens); `calls` whether the model calls the tool at all; `streams` whether it
    answers a request with stream in chunks (else it ignores stream)."""

    def __init__(self, renders=True, calls=True, reasoning="I will count the letters with the tool.", streams=False):
        self.renders, self.calls, self.reasoning, self.requests = renders, calls, reasoning, []
        self.streams = streams
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs.get("stream") and self.streams:
            return iter(chunks('{"word": "atlas"}'))
        messages = kwargs["messages"]
        prompt = 100 + sum(len(m.get("content") or "") for m in messages) // 4
        if self.renders:
            prompt += sum(len(m.get("reasoning") or "") for m in messages) // 4
        if len(messages) == 2 and self.calls:
            call = SimpleNamespace(id="c1", type="function",
                                   function=SimpleNamespace(name="word_length", arguments=json.dumps({"word": "atlas"})))
            message = SimpleNamespace(content=None, tool_calls=[call], reasoning=self.reasoning)
        elif len(messages) == 2:
            message = SimpleNamespace(content="Five, I think.", tool_calls=None)
        else:
            message = SimpleNamespace(content="The word atlas has 5 letters.", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(prompt_tokens=prompt))


def chunks(arguments):
    """A tool call in chunks, as a server streams it: the id and the name, then the arguments
    in pieces, then finish_reason."""
    def chunk(delta, finish=None):
        return SimpleNamespace(id="s", choices=[SimpleNamespace(delta=delta, finish_reason=finish)])
    call = lambda **f: SimpleNamespace(content=None, tool_calls=[SimpleNamespace(index=0, **f)])
    yield chunk(SimpleNamespace(content=None, tool_calls=None, reasoning="I will count."))
    yield chunk(call(id="c9", function=SimpleNamespace(name="word_length", arguments="")))
    for i in range(0, len(arguments), 5):
        yield chunk(call(id=None, function=SimpleNamespace(name=None, arguments=arguments[i:i + 5])))
    yield chunk(SimpleNamespace(content=None, tool_calls=None), "tool_calls")


def run(server, **service):
    lines = []
    code = probe(ServiceConfig(model="gemma-4", **service), client=server, out=lines.append)
    return code, "\n".join(lines)


def test_a_model_that_calls_the_tool_and_a_server_that_uses_the_reasoning():
    server = Server(reasoning="I will count the letters with the tool. " * 10, streams=True)
    code, out = run(server, reasoning_effort="none")
    assert code == 0
    assert "1 OK" in out and 'word_length({"word": "atlas"})' in out and "it reasoned (400 characters)" in out
    assert "2 OK" in out and "5 letters" in out
    assert "3 OK: the server puts the reasoning back" in out
    assert 'streaming: ok' in out and 'the same call word_length({"word": "atlas"})' in out
    first, second, third, fourth = server.requests
    assert fourth["stream"] is True and fourth["stream_options"] == {"include_usage": True}
    assert fourth["messages"] == first["messages"] and fourth["tools"] == first["tools"]
    assert first["reasoning_effort"] == "low"                       # "none" in [llm]: agents that write code reason
    assert second["messages"][2]["reasoning"].startswith("I will count")
    assert second["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": '{"word": "atlas", "letters": 5}'}
    assert "reasoning" not in third["messages"][2]


def test_a_server_that_drops_the_reasoning_and_a_model_that_does_not_call_tools():
    code, out = run(Server(renders=False, reasoning="Counting. " * 30), reasoning_field="reasoning")
    assert code == 0 and "3 NOT USED" in out and 'reasoning_field = "reasoning_content"' in out
    assert "streaming: not supported (it answered without streaming)" in out
    code, out = run(Server(), reasoning_field="")
    assert code == 0 and "3 skipped: reasoning_field is empty" in out
    server = Server(calls=False)
    code, out = run(server)
    assert code == 1 and "1 FAILED" in out and "did not call the tool" in out and "Five, I think." in out
    assert "streaming: not supported" in out and len(server.requests) == 2
    code, out = run(Server(calls=False, streams=True))
    assert code == 1 and "streaming: ok (" in out and "the same call" not in out
