"""atlas probe: the chat model as an agent, with a stub of the OpenAI client."""
import json
from types import SimpleNamespace

from atlas.llm import ServiceConfig
from atlas.llm.probe import probe


class Server:
    """Answers the probe: a tool call (with reasoning) and then the answer. `renders` says
    whether its chat template puts back the reasoning of the assistant message (it shows in
    prompt_tokens); `calls` whether the model calls the tool at all."""

    def __init__(self, renders=True, calls=True, reasoning="I will count the letters with the tool."):
        self.renders, self.calls, self.reasoning, self.requests = renders, calls, reasoning, []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
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


def run(server, **service):
    lines = []
    code = probe(ServiceConfig(model="gemma-4", **service), client=server, out=lines.append)
    return code, "\n".join(lines)


def test_a_model_that_calls_the_tool_and_a_server_that_uses_the_reasoning():
    server = Server(reasoning="I will count the letters with the tool. " * 10)
    code, out = run(server, reasoning_effort="none")
    assert code == 0
    assert "1 OK" in out and 'word_length({"word": "atlas"})' in out and "it reasoned (400 characters)" in out
    assert "2 OK" in out and "5 letters" in out
    assert "3 OK: the server puts the reasoning back" in out
    first, second, third = server.requests
    assert first["reasoning_effort"] == "low"                       # "none" in [llm]: agents that write code reason
    assert second["messages"][2]["reasoning"].startswith("I will count")
    assert second["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content":
                                  '<tool_result name="word_length">\n{"word": "atlas", "letters": 5}\n</tool_result>'}
    assert "reasoning" not in third["messages"][2]


def test_a_server_that_drops_the_reasoning_and_a_model_that_does_not_call_tools():
    code, out = run(Server(renders=False, reasoning="Counting. " * 30), reasoning_field="reasoning")
    assert code == 0 and "3 NOT USED" in out and 'reasoning_field = "reasoning_content"' in out
    code, out = run(Server(), reasoning_field="")
    assert code == 0 and "3 skipped: reasoning_field is empty" in out
    code, out = run(Server(calls=False))
    assert code == 1 and "1 FAILED" in out and "did not call the tool" in out and "Five, I think." in out
