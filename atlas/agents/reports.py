"""Reports written by an agent from what the graph finds without a model (the flows of the
plan: the steps are fixed, and the model only writes at the end).

    impact_report("Logger.Log", cfg)       the impact_analyst: what a change affects
    coverage_report("SRS_TMS", cfg)        the coverage_auditor: the traceability gaps

The data comes from the graph (impact, matrix, gaps); the agent of each Markdown file writes
the report with its instructions and model. The report keeps, after the text of the model,
the data it was written from, so that nothing depends on what the model chose to cite. Both
are written to <workspace>/reports. The model sees the graph without the text of the
local-only sources (KnowledgeGraph.for_model): their IDs and links, not what they say.
"""
from __future__ import annotations

import json
from typing import Any

from atlas.config import Config
from atlas.pipeline import atomic_write

__all__ = ["coverage_report", "impact_report"]

SHOWN = 15      # items of each list of gaps that the model sees; the report keeps them all


def _writer(name: str, cfg: Config, llm: Any):
    from atlas.agents.runtime import agent_service, definition
    d = definition(name, cfg)
    if llm is None:
        from atlas.llm import JsonLlm
        llm = JsonLlm(agent_service(d, cfg))
    return d, llm


def _record(cfg: Config, agent, nodes: list[str], g) -> None:
    """The record of what a report sends: the sources of the nodes in its data."""
    from atlas.agents.runtime import agent_service
    from atlas.outbound import record_sent
    labels = set()
    for n in nodes:
        d = g.G.nodes.get(n, {})
        if d.get("local_only"):             # only its identifier goes
            continue
        if d.get("module"):
            labels.add(f"doors:{d['module']}")
        elif d.get("kind") == "section":
            labels.add(f"docs:{g.G.nodes.get('doc:' + str(d.get('document')), {}).get('source', '?')}")
        elif d.get("file") or d.get("kind") == "unit":
            labels.add("code")
    record_sent(cfg.workspace / "atlas.db", agent_service(agent, cfg).model, labels, "reports")


def _file(cfg: Config, kind: str, target: str):
    name = "".join(c if c.isalnum() or c in "-_." else "_" for c in target) or "all"
    return cfg.workspace / "reports" / f"{kind}-{name}.md"


def _impact_data(g, impact: dict) -> str:
    lines = [f"Changed element: {impact['node']} ({impact['kind']})", "", "Code that depends on it:"]
    lines += [f"- {i['id']} ({i['kind']}, distance {i['distance']}, {i.get('file', '')})"
              + (f": {i['doc']}" if i.get("doc") else "") for i in impact["items"]] or ["- none"]
    lines += ["", "Requirements affected:"]
    lines += [f"- {r['id']}" + (f" ({r['module']})" if r.get("module") else " (not in DOORS)")
              + (f": {r['text']}" if r.get("text") else "") + f" [through {', '.join(r['via'])}]"
              for r in impact["requirements"]] or ["- none"]
    lines += ["", "Tests that exercise it or verify those requirements:"]
    lines += [f"- {t}" for t in impact["tests"]] or ["- none"]
    lines += ["", "Document sections:"]
    for x in impact["documents"]:
        node = g.G.nodes[x["id"]]
        statement = next((r["text"] for r in node.get("requirements", []) if r["id"] == x["via"]), "")
        lines.append(f"- {x['document']} › {x['title']} (through {x['via']})"
                     + (f": {statement}" if statement else ""))
    if not impact["documents"]:
        lines.append("- none")
    return "\n".join(lines)


def impact_report(name: str, cfg: Config, llm: Any = None, graph=None, depth: int = 3) -> dict:
    """What changing `name` (a code element or a requirement) affects, written by the agent
    impact_analyst. {"node", "report", "file", "impact"} or {"error"}."""
    from atlas.graph import KnowledgeGraph
    from atlas.outbound import LocalOnly
    g = (graph or KnowledgeGraph.from_config(cfg)).for_model(LocalOnly.from_config(cfg))
    impact = g.impact(name, depth=depth, limit=60)
    if "error" in impact:
        return impact
    if impact["kind"] in ("requirement", "heading", "information"):     # the code of the requirements derived
        impact = {**impact, "items": [{**i, "distance": 1} for i in impact["items"]],
                  "requirements": [{**r, "via": [impact["node"]]} for r in impact["requirements"]]}
    data = _impact_data(g, impact)
    agent, llm = _writer("impact_analyst", cfg, llm)
    _record(cfg, agent, [i["id"] for i in impact["items"]] + [impact["node"]]
            + [r["id"] for r in impact["requirements"]] + [x["id"] for x in impact["documents"]], g)
    text = llm.ask_text(agent.instruction, f"Impact analysis of a change to {impact['node']}.\n\n{data}")
    path = _file(cfg, "impact", impact["node"])
    atomic_write(path, f"{text.strip()}\n\n---\n\n## Data of the graph\n\n{data}\n")
    return {"node": impact["node"], "report": text, "file": str(path), "impact": impact}


def coverage_report(module: str, cfg: Config, llm: Any = None, graph=None) -> dict:
    """The traceability gaps of a DOORS module ("" = every requirement), summarised by the
    agent coverage_auditor. {"module", "report", "file", "summary", "gaps"} or {"error"}."""
    from atlas.graph import KnowledgeGraph
    from atlas.outbound import LocalOnly
    g = (graph or KnowledgeGraph.from_config(cfg)).for_model(LocalOnly.from_config(cfg))
    matrix = g.matrix(module, limit=0)
    if "error" in matrix:
        return matrix
    gaps = g.gaps(module, limit=10**6)
    lists = {k: v for k, v in gaps.items() if isinstance(v, dict)}

    def data(shown: int) -> str:
        out = [f"Module: {matrix['module']}", f"Summary: {json.dumps(matrix['summary'])}"]
        for key, part in lists.items():
            items = [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in part["items"]]
            listed = "\n".join(f"- {x}" for x in items[:shown]) or "- none"
            more = f"\n- … and {len(items) - shown} more" if len(items) > shown else ""
            out.append(f"\n{key.replace('_', ' ')} ({part['count']}):\n{listed}{more}")
        return "\n".join(out)

    agent, llm = _writer("coverage_auditor", cfg, llm)       # the model sees the first ones of each list
    reqs, _ = g._module_requirements(module)
    _record(cfg, agent, reqs, g)
    text = llm.ask_text(agent.instruction, f"Traceability audit of {matrix['module']}.\n\n{data(SHOWN)}")
    path = _file(cfg, "coverage", module or "all")
    atomic_write(path, f"{text.strip()}\n\n---\n\n## Data of the graph\n\n{data(10**6)}\n")
    return {"module": matrix["module"], "report": text, "file": str(path), "summary": matrix["summary"],
            "gaps": {k: v["count"] for k, v in lists.items()}}
