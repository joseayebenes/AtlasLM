"""Publishes the raw copy of DOORS (raw_store.py) to the requirements exchange database of
atlas (atlas/doors/exchange.py), applying the mapping of a mapping.toml:

    python tools/doors_export/publish.py --raw doors_raw.sqlite --mapping mapping.toml \\
                                         --out requirements.sqlite

The mapping says which DOORS attribute gives the ID, the text and every attribute of the
exchange database, which name every module takes in atlas, which link type every link
module is and, optionally, which attribute tells requirements from headings. It does not
need DOORS: changing the mapping only needs publishing again.

Every completely downloaded module is published whole (one transaction per module):
- the ID is the first one of [fields] id that the object has: the requirement ID and, for
  the objects without it, another one; objects without any are left out (and counted), and
  a repeated ID keeps the first one (the others are listed);
- the kind (requirement, heading, information) comes from the [kinds] attribute in a
  module that has it, and from the content otherwise;
- the parent is the nearest ancestor with an ID;
- a link is published if its target object is in the raw copy and has an ID; the others are
  counted, and the modules of their targets that are not downloaded are listed;
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
from collections import Counter
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

BUILT_IN = {"identifier", "absolute_number", "module_number"}    # what `id` can be besides a DOORS attribute
OTHERS = (*KINDS, "heading_or_information")                       # what [kinds] others can be


class MappingError(ValueError):
    pass


def _listed(items: list[str], shown: int = 10) -> str:
    """The first items, and how many more."""
    return ", ".join(items[:shown]) + (f" and {len(items) - shown} more" if len(items) > shown else "")


def _value_key(value: str) -> str:
    """A value of the [kinds] attribute as it is compared: without case or extra spaces."""
    return " ".join(value.split()).casefold()


def _invert(section: str, table: dict, path, key=lambda d: d) -> dict[str, str]:
    """{atlas: DOORS or [DOORS...]} -> {key(DOORS): atlas}: in mapping.toml atlas is always on
    the left and DOORS on the right, and one atlas value may gather several DOORS ones."""
    out: dict[str, str] = {}
    for atlas_value, doors in table.items():
        for d in ([doors] if not isinstance(doors, list) else doors):
            if not isinstance(d, str):
                raise MappingError(f"{path}: [{section}] {atlas_value} must be a text or a list of texts")
            if key(d) in out and out[key(d)] != atlas_value:
                raise MappingError(f"{path}: [{section}] {d!r} is given to {out[key(d)]} and to {atlas_value}")
            out[key(d)] = atlas_value
    return out


@dataclass
class Mapping:
    """What mapping.toml says (see mapping.example.toml), ready to look up by DOORS name."""
    id: tuple[str, ...] = ("identifier",)                         # the first one the object has
    text: str = "Object Text"
    heading: str = "Object Heading"
    attributes: dict[str, str] = field(default_factory=dict)      # atlas attribute -> DOORS attribute
    modules: dict[str, str] = field(default_factory=dict)         # DOORS module (path or name) -> atlas name
    link_types: dict[str, str] = field(default_factory=dict)      # link module (path or name) -> link type
    default_link_type: str = ""                                   # "" = the link module name
    kind_attribute: str = ""                                      # "" = heading without text is a heading
    kind_values: dict[str, str] = field(default_factory=dict)     # value of kind_attribute (_value_key) -> kind
    kind_others: str = ""                                         # kind of the other values; "" = requirement

    SECTIONS = {"fields": {"id", "text", "heading"}, "attributes": None, "modules": None, "link_types": None,
                "links": {"default_type"}, "kinds": {"attribute", "values", "others"}}

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
        ids = fields.get("id", "identifier")
        ids = [ids] if isinstance(ids, str) else ids
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i.strip() for i in ids):
            raise MappingError(f"{path}: [fields] id must be a text or a list of texts")
        bad = sorted(set(kinds.get("values", {})) - set(KINDS))
        if bad:
            raise MappingError(f"{path}: [kinds] values has kinds {bad} (valid: {', '.join(KINDS)})")
        if kinds.get("others", "requirement") not in OTHERS:
            raise MappingError(f"{path}: [kinds] others is {kinds['others']!r} (valid: {', '.join(OTHERS)})")
        if (kinds.get("values") or "others" in kinds) and not kinds.get("attribute"):
            raise MappingError(f"{path}: [kinds] values and others need attribute, the DOORS attribute they are of")
        return cls(id=tuple(ids), text=fields.get("text", "Object Text"),
                   heading=fields.get("heading", "Object Heading"), attributes=dict(data.get("attributes", {})),
                   modules=_invert("modules", data.get("modules", {}), path),
                   link_types=_invert("link_types", data.get("link_types", {}), path),
                   default_link_type=data.get("links", {}).get("default_type", ""),
                   kind_attribute=kinds.get("attribute", ""),
                   kind_values=_invert("kinds", kinds.get("values", {}), path, key=_value_key),
                   kind_others=kinds.get("others", ""))

    def doors_attributes(self) -> set[str]:
        """Every DOORS attribute the mapping reads."""
        names = {self.text, self.heading, *self.attributes.values()} | (set(self.id) - BUILT_IN)
        return {n for n in names | {self.kind_attribute} if n}

    def module_name(self, path: str) -> str:
        last = path.rstrip("/").rsplit("/", 1)[-1]
        return self.modules.get(path) or self.modules.get(last) or last

    def link_type(self, link_module: str) -> str:
        last = link_module.rstrip("/").rsplit("/", 1)[-1]
        found = self.link_types.get(link_module) or self.link_types.get(last)
        return found or self.default_link_type or ("_".join(last.lower().split()) if last else "link")

    def kind(self, attributes: dict[str, str], by_attribute: bool = True) -> str:
        """By the [kinds] attribute if the mapping has one and the module too; otherwise, by
        the content (`attributes`: the values of the object by their name in the source)."""
        text, heading = attributes.get(self.text, "").strip(), attributes.get(self.heading, "").strip()
        if not (self.kind_attribute and by_attribute):
            return "requirement" if text else "heading" if heading else "information"
        found = self.kind_values.get(_value_key(attributes.get(self.kind_attribute, "")))
        if found:
            return found
        if self.kind_others == "heading_or_information":           # the attribute only tells the requirements
            return "heading" if heading and not text else "information"
        return self.kind_others or "requirement"


@dataclass
class Report:
    MISSING = "modules not downloaded"          # where the targets of the links left out are
    KEY = "Absolute Number"                     # what tells apart the objects of a repeated ID

    module: str
    path: str
    requirements: int = 0
    links: int = 0
    without_id: int = 0
    repeated_ids: list[str] = field(default_factory=list)          # "ID (first, repeated)", by Absolute Number
    unresolved_links: int = 0
    not_downloaded: Counter = field(default_factory=Counter)       # module of link targets not downloaded -> links
    kinds: Counter = field(default_factory=Counter)                # objects per kind
    other_values: Counter = field(default_factory=Counter)         # values of the [kinds] attribute without a kind
    other_ids: list[str] = field(default_factory=list)             # requirements without the first [fields] id
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return f"{self.module} ({self.path}): FAILED: {self.error}"
        missing = sum(self.not_downloaded.values())
        extra = [f"{n} {what}" for n, what in ((self.without_id, "objects without an ID left out"),
                                               (len(self.repeated_ids), "repeated IDs left out"),
                                               (missing, f"links to {self.MISSING}"),
                                               (self.unresolved_links - missing, "links to objects without an ID or deleted"))
                 if n]
        kinds = ", ".join(f"{k} {self.kinds[k]}" for k in KINDS if self.kinds[k])
        return (f"{self.module} ({self.path}): {self.requirements} objects" + (f" ({kinds})" if kinds else "")
                + f", {self.links} links" + (f"; {', '.join(extra)}" if extra else ""))


class Publisher:
    def __init__(self, store: RawStore, mapping: Mapping):
        self.store, self.mapping = store, mapping
        self._ids: dict[str, dict[int, str]] = {}          # module path -> absolute number -> ID
        self.downloaded = set(store.modules(complete_only=False))

    def id_of(self, obj: DoorsObject, sources: tuple[str, ...] = ()) -> str:
        """The first ID the object has of [fields] id (or of `sources`)."""
        built_in = {"identifier": obj.identifier, "absolute_number": str(obj.absolute_number),
                    "module_number": f"{self.mapping.module_name(obj.module_path)}-{obj.absolute_number}"}
        for source in sources or self.mapping.id:
            if value := " ".join(built_in.get(source, obj.attributes.get(source, "")).split()):
                return value
        return ""

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
        notes = []
        for p in paths:
            missing = self.mapping.doors_attributes() - names[p]
            if missing:
                notes.append(f"{p}: has no {', '.join(sorted(missing))}"
                             + ("; its kinds come from the content" if self.mapping.kind_attribute in missing else ""))
        return notes

    def kind(self, obj: DoorsObject, by_attribute: bool = True) -> str:
        return self.mapping.kind(obj.attributes, by_attribute)

    def module(self, path: str) -> tuple[list[dict], list[dict], Report]:
        """The requirements and links of a module, as the exchange database wants them."""
        m, report = self.mapping, Report(module=self.mapping.module_name(path), path=path)
        objects = self.store.objects(path)
        by_number = {o.absolute_number: o for o in objects}
        by_attribute = bool(m.kind_attribute) and m.kind_attribute in self.store.attribute_names(path)
        ids, seen, requirements, links = self.ids(path), {}, [], []
        for position, obj in enumerate(objects, 1):
            rid = ids.get(obj.absolute_number, "")
            if not rid:
                report.without_id += 1
                continue
            if rid in seen:
                report.repeated_ids.append(f"{rid} ({seen[rid]}, {obj.absolute_number})")
                continue
            seen[rid] = obj.absolute_number
            parent, up, visited = None, obj.parent_absolute_number, set()
            while up is not None and up not in visited and parent is None:    # nearest ancestor with an ID
                visited.add(up)
                parent = ids.get(up)
                up = by_number[up].parent_absolute_number if up in by_number else None
            kind = self.kind(obj, by_attribute)
            report.kinds[kind] += 1
            if by_attribute and _value_key(value := obj.attributes.get(m.kind_attribute, "")) not in m.kind_values:
                report.other_values[" ".join(value.split())] += 1
            if kind == "requirement" and len(m.id) > 1 and not self.id_of(obj, m.id[:1]):
                report.other_ids.append(rid)
            requirements.append({
                "id": rid, "text": obj.attributes.get(m.text, ""), "heading": obj.attributes.get(m.heading, ""),
                "kind": kind, "parent": parent, "position": position, "level": obj.level or 1,
                "outline": obj.outline_number, "source_key": str(obj.absolute_number),
                "attributes": {name: obj.attributes.get(doors, "") for name, doors in m.attributes.items()}})
            for link in obj.links:
                target = self.ids(link.target_module).get(link.target_absolute_number)
                if not target:
                    report.unresolved_links += 1
                    if link.target_module not in self.downloaded:
                        report.not_downloaded[link.target_module] += 1
                    continue
                links.append({"source": rid, "target": target, "type": m.link_type(link.link_module),
                              "target_module": m.module_name(link.target_module)})
        report.requirements, report.links = len(requirements), len({(l["source"], l["target"], l["type"]) for l in links})
        return requirements, links, report


def warn(report: Report, mapping: Mapping) -> None:
    """The warnings of a module before it is written (shared with tools/reqif_import)."""
    if report.other_values:          # expected with [kinds] others; without it, maybe a value left out
        (log.info if mapping.kind_others else log.warning)(
            "%s: values of %s not in [kinds.values], taken as %s: %s", report.module,
            mapping.kind_attribute, mapping.kind_others or "requirement",
            _listed([f"{v!r} ({n})" for v, n in report.other_values.most_common()]))
    if report.other_ids:             # documents and code cite requirements by the first one
        log.warning("%s: %d requirements without %s, published with another ID: %s", report.module,
                    len(report.other_ids), mapping.id[0], _listed(report.other_ids))
    if report.repeated_ids:
        log.warning("%s: %d repeated IDs left out (%s of the object published and of the one left out): %s",
                    report.module, len(report.repeated_ids), report.KEY, _listed(report.repeated_ids))
    if report.not_downloaded:        # published once those modules are there too
        log.info("%s: links to %s: %s", report.module, report.MISSING,
                 _listed([f"{p} ({n})" for p, n in report.not_downloaded.most_common()]))


def write(writer: ExchangeWriter, report: Report, requirements: list[dict], links: list[dict]) -> None:
    """Writes a module, with its warnings and its summary in the log."""
    try:
        report.warnings = writer.replace_module(report.module, requirements, links, source=report.path)
    except ExchangeError as e:
        report.error = str(e)
    for w in report.warnings:
        log.warning("%s", w)
    log.info("%s", report.summary())


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
                warn(report, mapping)
                write(writer, report, requirements, links)
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
