"""The documentation keeps up with the code: every key that atlas.toml accepts is in the
configuration guide, under its section, and the relative links of the documentation lead
to files that exist."""
import re
from dataclasses import fields
from pathlib import Path

import pytest

from atlas.config import AGENT_SCHEMA, CODE_SCHEMA, DOCS_SCHEMA, DOORS_SCHEMA, SCHEMA
from atlas.doors import DEFAULT_CONVENTION
from atlas.llm import NetConfig

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "configuracion.md"


def _sections() -> dict[str, str]:
    """'## [heading]' of the guide -> its text, down to the next '## '."""
    parts = re.split(r"^## ", GUIDE.read_text(encoding="utf-8"), flags=re.M)
    return {p.split("\n", 1)[0].strip(): p for p in parts[1:]}


KEYS = {
    "[project]": SCHEMA["project"],
    "[llm]": SCHEMA["llm"],
    "[embeddings]": SCHEMA["embeddings"],
    "[network]": {*SCHEMA["network"], *(f.name for f in fields(NetConfig))},
    "[pipeline]": SCHEMA["pipeline"],
    "[plugins]": SCHEMA["plugins"],
    "[[sources.docs]]": DOCS_SCHEMA,
    "[sources.doors]": {*DOORS_SCHEMA, *DEFAULT_CONVENTION, "path", "kinds", "driver", "dsn", "modules_query",
                        "requirements_query", "links_query", "snapshot_query"},
    "[sources.code]": CODE_SCHEMA,
    "[graph]": SCHEMA["graph"],
    "[index]": SCHEMA["index"],
    "[agents]": {*SCHEMA["agents"], *AGENT_SCHEMA, "sets", "runs"},
    "[mcp]": SCHEMA["mcp"],
}


@pytest.mark.parametrize("section", sorted(KEYS))
def test_every_key_is_in_the_configuration_guide(section):
    sections = _sections()
    assert section in sections, f"the guide has no '## {section}'"
    missing = sorted(k for k in KEYS[section] if f"`{k}`" not in sections[section])
    assert not missing, f"{section}: {missing} are not documented in {GUIDE.name}"


def test_the_sections_of_the_guide_are_the_ones_of_the_schema():
    documented = {s for s in _sections() if s.startswith("[")}
    assert documented == set(KEYS) and {s.strip("[]") for s in KEYS} >= set(SCHEMA) - {"sources"}


def test_relative_links_lead_to_files_that_exist():
    pages = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md")),
             *sorted((ROOT / "atlas").glob("**/README.md")), ROOT / "tools" / "doors_export" / "README.md"]
    broken = []
    for page in pages:
        for target in re.findall(r"\]\(([^)\s]+)\)", page.read_text(encoding="utf-8")):
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue
            if not (page.parent / target.split("#")[0]).exists():
                broken.append(f"{page.relative_to(ROOT)} -> {target}")
    assert not broken, broken
