"""An agent of atlas for the tools of ADK, to see its calls step by step in the browser:

    ATLAS_CONFIG=atlas.dev.toml adk web examples/adk        # then choose "atlas_agent"
    ATLAS_CONFIG=atlas.dev.toml adk run examples/adk/atlas_agent

ATLAS_CONFIG is the configuration (atlas.toml by default) and ATLAS_AGENT the agent, by the
name of its Markdown file (assistant by default). Run it from the folder of the project: the
paths of the configuration are relative to it.
"""
import os

from atlas.agents.runtime import build_agent
from atlas.config import load_config

root_agent = build_agent(os.environ.get("ATLAS_AGENT", "assistant"),
                         load_config(os.environ.get("ATLAS_CONFIG", "atlas.toml")))
