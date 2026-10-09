"""ADK agent against a local Ollama: real tool calling over the sample data.

Skipped unless the [agents] extra (google-adk, litellm) is installed and the Ollama of
atlas.dev.toml is running with its [agents] model pulled. Run only these with
`pytest -m ollama`, or leave them out with `pytest -m "not ollama"`.
"""
import asyncio
import json
import os
import re
import urllib.request
from pathlib import Path

import pytest

from atlas.config import load_config
from atlas.doors import DatabaseAdapter, normalize_id

ROOT = Path(__file__).resolve().parents[1]
QUESTION = ("Which system requirement (SSS) does SRS-0004 satisfy, and in which Ada files "
            "is SRS-0004 implemented and tested?")
SAMPLE_ADA_FILES = {"tracks.ads", "tracks.adb", "logger.ads", "logger.adb", "test_tracks.adb"}

pytestmark = [pytest.mark.ollama,                      # noise from ADK and litellm imports:
              pytest.mark.filterwarnings("ignore:.*EXPERIMENTAL.*:UserWarning"),
              pytest.mark.filterwarnings("ignore:.*ReadOnly.*:UserWarning")]

# litellm downloads its model price list on import unless told to use the bundled one.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


@pytest.fixture(scope="module")
def ollama():
    """(base_url, api_key, model) of atlas.dev.toml, or skip if it cannot be used."""
    pytest.importorskip("google.adk", reason="needs the [agents] extra")
    pytest.importorskip("litellm", reason="needs the [agents] extra")
    cfg = load_config(ROOT / "atlas.dev.toml")
    base_url, model = cfg["llm"]["base_url"], cfg["agents"]["model"]
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=2) as r:
            models = {m["id"] for m in json.load(r)["data"]}
    except OSError as e:
        pytest.skip(f"Ollama is not running at {base_url} ({e})")
    if model not in models and f"{model}:latest" not in models:
        pytest.skip(f"model {model} is not in Ollama (ollama pull {model})")
    return base_url, cfg["llm"]["api_key"], model


def _tools(doors_config: dict, ada_root: Path) -> list:
    adapter = DatabaseAdapter(doors_config)
    requirements = {r.id: r for m in adapter.modules() for r in adapter.requirements(m)}
    links = [link for m in adapter.modules() for link in adapter.links(m)]
    adapter.close()

    def get_requirement(req_id: str) -> dict:
        """Returns the text, module and links (outgoing and incoming) of a DOORS requirement."""
        rid = normalize_id(req_id)
        r = requirements.get(rid)
        if r is None:
            return {"error": f"{req_id} does not exist"}
        return {"id": r.id, "module": r.module, "text": r.text,
                "links_out": [{"type": l.type, "target": l.target} for l in links if l.source == rid],
                "links_in": [{"type": l.type, "source": l.source} for l in links if l.target == rid]}

    def trace(req_id: str) -> dict:
        """Finds the 'Req:' tags in the Ada code that cite the requirement; returns file and line."""
        rid = normalize_id(req_id)
        references = []
        for f in sorted(ada_root.rglob("*.ad[sb]")):
            for n, text in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if "Req:" in text and re.search(rf"\b{re.escape(rid)}\b", text):
                    references.append({"file": f.relative_to(ada_root).as_posix(), "line": n,
                                       "kind": "test" if "tests" in f.parts else "source"})
        return {"req_id": rid, "references": references}

    return [get_requirement, trace]


async def _ask(model, tools: list, question: str) -> tuple[list[str], str]:
    """Runs the question through an LlmAgent; returns (tools called, final answer)."""
    from google.adk.agents import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    agent = LlmAgent(
        name="assistant", model=model, tools=tools,
        instruction=("Answer traceability questions using ONLY the tools. Cite the exact "
                     "requirement IDs and file:line references. Be brief."))
    runner = InMemoryRunner(agent=agent, app_name="atlas-test")
    session = await runner.session_service.create_session(app_name="atlas-test", user_id="test")
    message = types.Content(role="user", parts=[types.Part(text=question)])
    calls, answer = [], ""
    async for event in runner.run_async(user_id="test", session_id=session.id, new_message=message):
        for part in (event.content.parts if event.content else None) or []:
            if part.function_call:
                calls.append(part.function_call.name)
            elif part.text and not part.thought and event.is_final_response():
                answer += part.text
    return calls, answer


def test_agent_traces_a_requirement_with_tools(ollama, toml, samples):
    from google.adk.models.lite_llm import LiteLlm

    base_url, api_key, model_name = ollama
    # gemma4 reasons by default. With reasoning on, 3 out of 10 runs ended with an empty
    # or invented answer; with it off, 5 out of 5 were right and about 4x faster.
    model = LiteLlm(model=f"openai/{model_name}", api_base=base_url, api_key=api_key,
                    reasoning_effort="none")
    tools = _tools(load_config(toml())["sources"]["doors"], samples / "ada")
    calls, answer = asyncio.run(asyncio.wait_for(_ask(model, tools, QUESTION), timeout=300))

    assert {"get_requirement", "trace"} <= set(calls), calls
    assert "SSS-0005" in answer, answer
    cited = set(re.findall(r"\w+\.ad[sb]", answer))
    assert {"tracks.ads", "test_tracks.adb"} <= cited, answer
    assert cited <= SAMPLE_ADA_FILES, f"cites files that do not exist: {answer}"
