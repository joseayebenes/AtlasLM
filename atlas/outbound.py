"""What goes out to the models: the sources whose text never leaves the workstation, and the
record of what has been sent to each model.

Local-only sources (`model = "none"`): a [[sources.docs]], some or all DOORS modules
([sources.doors] model, local_only) and the code ([sources.code] model). Their text is never
analysed by the model nor embedded; it is in the graph and in the lexical and ID searches,
and the tools that do not call a model (search, get_section, trace, read_source,
build_context through MCP) give it whole. What calls a model leaves it out of the prompt and
says what it left out (`local_only`).

    local = LocalOnly.from_config(cfg)
    local.index_doc("doors:SRS_CLIENT")      # True if that module is local-only
    local.node(G, "tracks.delete_track")     # a node of the graph

Identifiers (requirement IDs, code names, file names) are not text of a source: they may go
in a prompt to say what was left out.
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["MODELS", "LocalOnly", "source_label"]

MODELS = ("full", "none")          # model = ...: "none" keeps the text of a source in the workstation
DOORS_PREFIX = "doors:"            # the keys of the index (atlas.index.records)
CODE_PREFIX = "code:"
CODE_KINDS = ("subprogram", "test", "type", "variable", "constant", "unit", "external")
REQUIREMENT_KINDS = ("requirement", "heading", "information")


def source_label(doc: str) -> str:
    """The source of a document of the index, as the record names it: "docs:<source>" for a
    document (its key is "<source>:<path>"), "doors:<module>", or "code"."""
    if doc.startswith(DOORS_PREFIX):
        return doc
    if doc.startswith(CODE_PREFIX):
        return "code"
    return "docs:" + doc.split(":", 1)[0]


@dataclass(frozen=True)
class LocalOnly:
    """The local-only sources of a configuration."""
    docs: frozenset = field(default_factory=frozenset)       # names of [[sources.docs]]
    modules: frozenset = field(default_factory=frozenset)    # DOORS modules
    all_modules: bool = False                                 # [sources.doors] model = "none"
    code: bool = False                                        # [sources.code] model = "none"

    @classmethod
    def from_config(cls, cfg) -> "LocalOnly":
        sources = cfg["sources"] if cfg is not None else {}
        doors, code = sources.get("doors") or {}, sources.get("code") or {}
        return cls(docs=frozenset(d["name"] for d in sources.get("docs", []) if d.get("model") == "none"),
                   modules=frozenset(doors.get("local_only", [])), all_modules=doors.get("model") == "none",
                   code=code.get("model") == "none")

    def __bool__(self) -> bool:
        return bool(self.docs or self.modules or self.all_modules or self.code)

    def module(self, module: str | None) -> bool:
        return bool(module) and (self.all_modules or module in self.modules)

    def source(self, name: str) -> bool:
        """A [[sources.docs]] by its name."""
        return name in self.docs

    def index_doc(self, doc: str) -> bool:
        """A document of the index by its key: "<source>:<path>", "doors:<module>", "code:<unit>"."""
        if doc.startswith(DOORS_PREFIX):
            return self.module(doc[len(DOORS_PREFIX):])
        if doc.startswith(CODE_PREFIX):
            return self.code
        return self.source(doc.split(":", 1)[0])

    def node(self, G, n: str) -> bool:
        """A node of the knowledge graph: a requirement of a local module, a document or
        section of a local source, or code when the code is local."""
        d = G.nodes[n]
        kind = d.get("kind")
        if kind in REQUIREMENT_KINDS:
            return self.module(d.get("module"))
        if kind == "document":
            return self.source(d.get("source", ""))
        if kind == "section":
            doc = f"doc:{d.get('document')}"
            return doc in G and self.source(G.nodes[doc].get("source", ""))
        return self.code and kind in CODE_KINDS

    def describe(self) -> list[str]:
        """The local-only sources, for a person."""
        out = [f"docs:{n}" for n in sorted(self.docs)]
        out += ["doors (every module)"] if self.all_modules else [f"doors:{m}" for m in sorted(self.modules)]
        return out + (["code"] if self.code else [])

