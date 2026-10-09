"""Publishes the raw copy of DOORS (raw_store.py) to the requirements exchange database of
atlas (atlas/doors/exchange.py), applying the mapping of a mapping.toml:

    python tools/doors_export/publish.py --raw doors_raw.sqlite --mapping mapping.toml \\
                                         --out requirements.sqlite

The mapping says which DOORS attribute gives the ID, the text and every attribute of the
exchange database, which name every module takes in atlas, which link type every link
module is and, optionally, which attribute tells requirements from headings. It does not
need DOORS: changing the mapping only needs publishing again.

Every completely downloaded module is published whole (one transaction per module):
- objects without an ID are left out (and counted); a repeated ID keeps the first one;
- the parent is the nearest ancestor with an ID;
- a link is published if its target object is in the raw copy and has an ID;
- a mapped attribute that no module has is an error, with the closest names.
The exchange database belongs to this publication: modules published before and no
longer downloaded are removed, unless --keep-others.
"""
from __future__ import annotations

import argparse
import difflib
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from raw_store import DoorsObject, RawStore  # noqa: E402

# The contract of the same checkout first, so that writer and reader always agree; out of a
# checkout, the installed atlas.
_REPO = Path(__file__).resolve().parents[2]
if (_REPO / "atlas" / "doors" / "exchange.py").exists():
    sys.path.insert(0, str(_REPO))
from atlas.doors.exchange import KINDS, ExchangeError, ExchangeWriter  # noqa: E402

try:
    import tomllib
except ImportError:                       # Python < 3.11
    import tomli as tomllib  # type: ignore

log = logging.getLogger("doors-publish")

BUILT_IN = {"identifier", "absolute_number"}     # what `id` can be besides a DOORS attribute


class MappingError(ValueError):
    pass


def _invert(section: str, table: dict, path) -> dict[str, str]:
    """{atlas: DOORS or [DOORS...]} -> {DOORS: atlas}: in mapping.toml atlas is always on
    the left and DOORS on the right, and one atlas value may gather several DOORS ones."""
    out: dict[str, str] = {}
    for atlas_value, doors in table.items():
        for d in ([doors] if isinstance(doors, str) else doors):
            if not isinstance(d, str):
                raise MappingError(f"{path}: [{section}] {atlas_value} must be a text or a list of texts")
            if d in out and out[d] != atlas_value:
                raise MappingError(f"{path}: [{section}] {d!r} is given to {out[d]} and to {atlas_value}")
            out[d] = atlas_value
    return out


@dataclass
class Mapping:
    """What mapping.toml says (see mapping.example.toml), ready to look up by DOORS name."""
    id: str = "identifier"
    text: str = "Object Text"
    heading: str = "Object Heading"
    attributes: dict[str, str] = field(default_factory=dict)      # atlas attribute -> DOORS attribute
    modules: dict[str, str] = field(default_factory=dict)         # DOORS module (path or name) -> atlas name
    link_types: dict[str, str] = field(default_factory=dict)      # link module (path or name) -> link type
    default_link_type: str = ""                                   # "" = the link module name
    kind_attribute: str = ""                                      # "" = heading without text is a heading
    kind_values: dict[str, str] = field(default_factory=dict)     # value of kind_attribute -> kind

    SECTIONS = {"fields": {"id", "text", "heading"}, "attributes": None, "modules": None, "link_types": None,
                "links": {"default_type"}, "kinds": {"attribute", "values"}}

    @classmethod
    def load(cls, path: str | Path) -> "Mapping":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for section, value in data.items():
            if section not in cls.SECTIONS:
                raise MappingError(f"{path}: unknown section [{section}] (valid: {', '.join(cls.SECTIONS)})")
            if not isinstance(value, dict):
                raise MappingError(f"{path}: [{section}] must be a table")
            keys = cls.SECTIONS[section]
            unknown = sorted(set(value) - keys) if keys else []
            if unknown:
                raise MappingError(f"{path}: unknown keys {unknown} in [{section}] (valid: {', '.join(sorted(keys))})")
        fields, kinds = data.get("fields", {}), data.get("kinds", {})
        bad = sorted(set(kinds.get("values", {})) - set(KINDS))
        if bad:
            raise MappingError(f"{path}: [kinds] values has kinds {bad} (valid: {', '.join(KINDS)})")
        return cls(id=fields.get("id", "identifier"), text=fields.get("text", "Object Text"),
                   heading=fields.get("heading", "Object Heading"), attributes=dict(data.get("attributes", {})),
                   modules=_invert("modules", data.get("modules", {}), path),
                   link_types=_invert("link_types", data.get("link_types", {}), path),
                   default_link_type=data.get("links", {}).get("default_type", ""),
                   kind_attribute=kinds.get("attribute", ""),
                   kind_values=_invert("kinds", kinds.get("values", {}), path))

    def doors_attributes(self) -> set[str]:
        """Every DOORS attribute the mapping reads."""
        names = {self.text, self.heading, *self.attributes.values()} | ({self.id} - BUILT_IN)
        return {n for n in names | {self.kind_attribute} if n}

    def module_name(self, path: str) -> str:
        last = path.rstrip("/").rsplit("/", 1)[-1]
        return self.modules.get(path) or self.modules.get(last) or last

    def link_type(self, link_module: str) -> str:
        last = link_module.rstrip("/").rsplit("/", 1)[-1]
        found = self.link_types.get(link_module) or self.link_types.get(last)
        return found or self.default_link_type or ("_".join(last.lower().split()) if last else "link")


@dataclass
class Report:
    module: str
    path: str
    requirements: int = 0
    links: int = 0
    without_id: int = 0
    repeated_ids: int = 0
    unresolved_links: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return f"{self.module} ({self.path}): FAILED: {self.error}"
        extra = [f"{n} {what}" for n, what in ((self.without_id, "objects without an ID left out"),
                                               (self.repeated_ids, "repeated IDs left out"),
                                               (self.unresolved_links, "links to objects not downloaded"))
                 if n]
        return (f"{self.module} ({self.path}): {self.requirements} objects, {self.links} links"
                + (f"; {', '.join(extra)}" if extra else ""))


class Publisher:
    def __init__(self, store: RawStore, mapping: Mapping):
        self.store, self.mapping = store, mapping
        self._ids: dict[str, dict[int, str]] = {}          # module path -> absolute number -> ID

    def id_of(self, obj: DoorsObject) -> str:
        m = self.mapping.id
        value = obj.identifier if m == "identifier" else str(obj.absolute_number) if m == "absolute_number" \
            else obj.attributes.get(m, "")
        return " ".join(value.split())

    def ids(self, path: str) -> dict[int, str]:
        if path not in self._ids:
            self._ids[path] = {o.absolute_number: i for o in self.store.objects(path) if (i := self.id_of(o))}
        return self._ids[path]

    def check(self, paths: list[str]) -> list[str]:
        """Raises MappingError for an attribute no module has; warns per module."""
        names = {p: self.store.attribute_names(p) for p in paths}
        everywhere = set().union(*names.values()) if names else set()
        missing = sorted(self.mapping.doors_attributes() - everywhere)
        if missing:
            hints = [f"{n!r} (closest: {', '.join(difflib.get_close_matches(n, sorted(everywhere), 3, 0.5)) or '-'})"
                     for n in missing]
            raise MappingError(f"no downloaded module has the DOORS attributes {', '.join(hints)}")
        return [f"{p}: has no {', '.join(sorted(self.mapping.doors_attributes() - names[p]))}"
                for p in paths if self.mapping.doors_attributes() - names[p]]

    def kind(self, obj: DoorsObject) -> str:
        m = self.mapping
        if m.kind_attribute:
            return m.kind_values.get(obj.attributes.get(m.kind_attribute, ""), "requirement")
        text, heading = obj.attributes.get(m.text, "").strip(), obj.attributes.get(m.heading, "").strip()
        return "requirement" if text else "heading" if heading else "information"

    def module(self, path: str) -> tuple[list[dict], list[dict], Report]:
        """The requirements and links of a module, as the exchange database wants them."""
        m, report = self.mapping, Report(module=self.mapping.module_name(path), path=path)
        objects = self.store.objects(path)
        by_number = {o.absolute_number: o for o in objects}
        ids, seen, requirements, links = self.ids(path), set(), [], []
        for position, obj in enumerate(objects, 1):
            rid = ids.get(obj.absolute_number, "")
            if not rid:
                report.without_id += 1
                continue
            if rid in seen:
                report.repeated_ids += 1
                continue
            seen.add(rid)
            parent, up, visited = None, obj.parent_absolute_number, set()
            while up is not None and up not in visited and parent is None:    # nearest ancestor with an ID
                visited.add(up)
                parent = ids.get(up)
                up = by_number[up].parent_absolute_number if up in by_number else None
            requirements.append({
                "id": rid, "text": obj.attributes.get(m.text, ""), "heading": obj.attributes.get(m.heading, ""),
                "kind": self.kind(obj), "parent": parent, "position": position, "level": obj.level or 1,
                "outline": obj.outline_number, "source_key": str(obj.absolute_number),
                "attributes": {name: obj.attributes.get(doors, "") for name, doors in m.attributes.items()}})
            for link in obj.links:
                target = self.ids(link.target_module).get(link.target_absolute_number)
                if not target:
                    report.unresolved_links += 1
                    continue
                links.append({"source": rid, "target": target, "type": m.link_type(link.link_module),
                              "target_module": m.module_name(link.target_module)})
        report.requirements, report.links = len(requirements), len({(l["source"], l["target"], l["type"]) for l in links})
        return requirements, links, report


def publish(raw: str, mapping: Mapping, out: str, modules: list[str] | None = None,
            keep_others: bool = False) -> list[Report]:
    """Publishes every complete module of the raw copy (or the given ones)."""
    reports = []
    with RawStore(raw) as store:
        complete = store.modules(complete_only=True)
        paths = [p for p in complete if not modules or p in modules or mapping.module_name(p) in modules]
        for missing in sorted(set(modules or []) - set(paths) - {mapping.module_name(p) for p in paths}):
            log.warning("%s: not completely downloaded in %s; not published", missing, raw)
        publisher = Publisher(store, mapping)
        for warning in publisher.check(paths):
            log.warning("%s", warning)
        with ExchangeWriter(out, filler="tools/doors_export") as writer:
            if not keep_others and not modules:
                # before writing: a renamed module would otherwise clash with its old IDs
                for old in sorted(set(writer.modules()) - {mapping.module_name(p) for p in paths}):
                    log.info("%s: no longer published, removed from %s", old, out)
                    writer.remove_module(old)
            for path in paths:
                requirements, links, report = publisher.module(path)
                try:
                    report.warnings = writer.replace_module(report.module, requirements, links, source=path)
                except ExchangeError as e:
                    report.error = str(e)
                for w in report.warnings:
                    log.warning("%s", w)
                log.info("%s", report.summary())
                reports.append(report)
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publishes the raw copy of DOORS to the exchange database of atlas.")
    parser.add_argument("--raw", default=os.environ.get("DOORS_RAW_DB", "doors_raw.sqlite"), help="raw copy")
    parser.add_argument("--mapping", default="mapping.toml", help="mapping file (see mapping.example.toml)")
    parser.add_argument("--out", default="requirements.sqlite", help="exchange database to fill")
    parser.add_argument("--module", action="append", default=[], metavar="PATH_OR_NAME",
                        help="publish only this module; can be repeated")
    parser.add_argument("--keep-others", action="store_true",
                        help="do not remove the modules of the exchange database that are not published now")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        reports = publish(args.raw, Mapping.load(args.mapping), args.out, args.module, args.keep_others)
    except (MappingError, ExchangeError, FileNotFoundError) as e:
        log.error("%s", e)
        return 1
    if not reports:
        log.error("%s has no completely downloaded module to publish", args.raw)
        return 1
    print(f"\nExchange database: {args.out}")
    for r in reports:
        print(f"  {r.summary()}")
    return 1 if any(r.error for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
