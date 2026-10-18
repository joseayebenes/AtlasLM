"""Fills the requirements exchange database of atlas (atlas/doors/exchange.py) from ReqIF
files, the exchange format of DOORS, Polarion, Jama, codebeamer...:

    python tools/reqif_import/reqif_to_exchange.py SRS.reqif SSS.reqifz --mapping mapping.toml \\
                                                   --out requirements.sqlite

The second way to the exchange database, next to tools/doors_export: it needs neither
Windows, nor DOORS, nor pywin32, only the standard library (xml.etree, zipfile). The mapping
is the mapping.toml of tools/doors_export, read by its publish.py, with the ReqIF names on the
right: the LONG-NAME of the attribute definitions, of the specifications and of the relation
types.

    SPECIFICATION   a module: its LONG-NAME, renamed by [modules]
    SPEC-HIERARCHY  the order, the level (its depth) and the parent (the nearest ancestor
                    with an ID) of each object; the outline is numbered from it (1, 1.1, 1.2...)
    SPEC-OBJECT     a requirement: [fields] id, text and heading, [attributes] and [kinds], with
                    the attributes its SPEC-OBJECT-TYPE defines (those of another type are not
                    there, not even empty)
    SPEC-RELATION   a link from its source to its target, of the type of its
                    SPEC-RELATION-TYPE: its LONG-NAME, renamed by [link_types], or
                    [links] default_type, or its name in lower case with _

The ID is the first one of [fields] id that the object has: an attribute, or
"identifier" (the IDENTIFIER of the SPEC-OBJECT: in a ReqIF of DOORS an internal one, not the
ID of documents and code), "absolute_number" (ReqIF.ForeignID, the Absolute Number in DOORS)
or "module_number" (the module name in atlas and that number). "Object Text", "Object
Heading" and "Absolute Number" of a mapping written for DOORS also find ReqIF.Text,
ReqIF.ChapterName and ReqIF.ForeignID, the names of the ReqIF implementation guide.

The warnings are those of publish.py: objects without an ID and repeated IDs left out (by
their IDENTIFIER), values of the [kinds] attribute without a kind, requirements with another
ID, links whose target is in none of the files given (give the ReqIF of that module too),
plus the IDs that do not match --id-pattern. Several files are read together, so that the
links between their modules are resolved. As publish.py, the exchange database belongs to
this import: the modules imported before and not now are removed, unless --keep-others or
--module.
"""
from __future__ import annotations

import argparse
import difflib
import logging
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOLS / "doors_export"))

from publish import BUILT_IN, Mapping, MappingError, Report, _listed, _value_key, warn, write  # noqa: E402
from atlas.doors.exchange import ExchangeError, ExchangeWriter  # noqa: E402  (publish.py found it)

log = logging.getLogger("reqif-import")

# a mapping written for DOORS, over the names of the ReqIF implementation guide
ALIASES = {"object text": "ReqIF.Text", "object heading": "ReqIF.ChapterName", "absolute number": "ReqIF.ForeignID",
           "reqif.text": "Object Text", "reqif.chaptername": "Object Heading", "reqif.foreignid": "Absolute Number"}
FOREIGN_ID = "ReqIF.ForeignID"
BLOCKS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote", "pre",
          "dd", "dt"}


def _tag(e: ET.Element) -> str:
    return e.tag.rsplit("}", 1)[-1]


def _child(e: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in e if _tag(c) == name), None)


def _children(e: ET.Element | None, name: str = "") -> list[ET.Element]:
    return [c for c in (e if e is not None else []) if not name or _tag(c) == name]


def _ref(e: ET.Element, holder: str) -> str:
    """The text of the *-REF inside the child `holder` (TYPE, DEFINITION, SOURCE...)."""
    h = _child(e, holder)
    r = next(iter(h), None) if h is not None else None
    return (r.text or "").strip() if r is not None else ""


def xhtml_text(e: ET.Element) -> str:
    """The text of an XHTML value: a line per paragraph, list item or break; the other line
    breaks and spaces of the XML are one space (but in <pre>)."""
    out: list[str] = []

    def text(value: str | None, pre: bool) -> None:
        if value:
            out.append(value if pre else re.sub(r"\s+", " ", value))

    def walk(x: ET.Element, pre: bool) -> None:
        tag = _tag(x)
        pre = pre or tag == "pre"
        if tag in BLOCKS:
            out.append("\n")
        if tag != "object":                            # an embedded object (an image, OLE): its alternative text only
            text(x.text, pre)
        for c in x:
            walk(c, pre)
            text(c.tail, pre)
        if tag in BLOCKS:
            out.append("\n")

    text(e.text, False)
    for c in e:
        walk(c, False)
        text(c.tail, False)
    lines = [" ".join(line.split()) for line in "".join(out).split("\n")]
    return "\n".join(line for line in lines if line)


@dataclass
class SpecObject:
    identifier: str
    type: str                                       # IDENTIFIER of its SPEC-OBJECT-TYPE
    values: dict[str, str] = field(default_factory=dict)          # LONG-NAME of the attribute -> text


@dataclass
class Entry:
    """An object in the hierarchy of a specification."""
    object: str
    level: int
    position: int
    outline: str
    parent: int | None = None                       # index of the parent entry


@dataclass
class Specification:
    name: str
    source: str                                     # file#LONG-NAME
    entries: list[Entry] = field(default_factory=list)


@dataclass
class Content:
    """What the ReqIF files hold, together."""
    objects: dict[str, SpecObject] = field(default_factory=dict)
    type_attributes: dict[str, set[str]] = field(default_factory=dict)    # SPEC-OBJECT-TYPE -> its attributes
    relations: list[tuple[str, str, str]] = field(default_factory=list)  # (source, target, relation type name)
    specifications: list[Specification] = field(default_factory=list)

    def attribute_names(self, spec: Specification | None = None) -> set[str]:
        types = {self.objects[e.object].type for e in spec.entries if e.object in self.objects} if spec else \
            set(self.type_attributes)
        return set().union(*(self.type_attributes.get(t, set()) for t in types)) if types else set()


def _documents(path: Path) -> list[tuple[str, bytes]]:
    """The ReqIF documents of a file: itself, or the .reqif inside a .reqifz (a zip)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".reqif")]
            if not names:
                raise ValueError(f"{path} is a zip without .reqif files")
            return [(f"{path.name}/{n}", z.read(n)) for n in names]
    return [(path.name, path.read_bytes())]


def read_reqif(paths: list[str | Path]) -> Content:
    content = Content()
    for path in paths:
        for name, data in _documents(Path(path)):
            try:
                root = ET.fromstring(data)
            except ET.ParseError as e:
                raise ValueError(f"{name} is not valid XML: {e}") from None
            if _tag(root) != "REQ-IF":
                raise ValueError(f"{name} is not a ReqIF file (its root is {_tag(root)})")
            _read(root, name, content)
    return content


def _read(root: ET.Element, name: str, content: Content) -> None:
    body = next((e for e in root.iter() if _tag(e) == "REQ-IF-CONTENT"), None)
    if body is None:
        raise ValueError(f"{name} has no REQ-IF-CONTENT")
    sections = {_tag(c): c for c in body}
    enum_names: dict[str, str] = {}
    for e in _children(sections.get("DATATYPES")):
        for v in e.iter():
            if _tag(v) == "ENUM-VALUE":
                other = next((x.get("OTHER-CONTENT") for x in v.iter() if _tag(x) == "EMBEDDED-VALUE"), "")
                enum_names[v.get("IDENTIFIER", "")] = v.get("LONG-NAME") or other or v.get("IDENTIFIER", "")
    definitions: dict[str, str] = {}               # attribute definition -> its LONG-NAME
    defaults: dict[str, dict[str, str]] = {}        # SPEC-OBJECT-TYPE -> attribute -> default value
    relation_types: dict[str, str] = {}
    for t in _children(sections.get("SPEC-TYPES")):
        kind, ident = _tag(t), t.get("IDENTIFIER", "")
        if kind == "SPEC-RELATION-TYPE":
            relation_types[ident] = t.get("LONG-NAME") or ident
        if kind != "SPEC-OBJECT-TYPE":
            continue
        names = content.type_attributes.setdefault(ident, set())
        for d in _children(_child(t, "SPEC-ATTRIBUTES")):
            definitions[d.get("IDENTIFIER", "")] = long_name = d.get("LONG-NAME") or d.get("IDENTIFIER", "")
            names.add(long_name)
            default = _child(d, "DEFAULT-VALUE")
            value = next(iter(default), None) if default is not None else None
            if value is not None:
                defaults.setdefault(ident, {})[long_name] = _value(value, enum_names)
    for o in _children(sections.get("SPEC-OBJECTS"), "SPEC-OBJECT"):
        obj = SpecObject(o.get("IDENTIFIER", ""), _ref(o, "TYPE"))
        obj.values = dict(defaults.get(obj.type, {}))
        for v in _children(_child(o, "VALUES")):
            definition = _ref(v, "DEFINITION")
            obj.values[definitions.get(definition, definition)] = _value(v, enum_names)
        content.objects[obj.identifier] = obj
    for r in _children(sections.get("SPEC-RELATIONS"), "SPEC-RELATION"):
        kind = _ref(r, "TYPE")
        content.relations.append((_ref(r, "SOURCE"), _ref(r, "TARGET"), relation_types.get(kind, kind)))
    for s in _children(sections.get("SPECIFICATIONS"), "SPECIFICATION"):
        spec = Specification(s.get("LONG-NAME") or s.get("IDENTIFIER", ""), f"{name}#{s.get('LONG-NAME', '')}")
        _hierarchy(spec, _child(s, "CHILDREN"), 1, None, "")
        content.specifications.append(spec)


def _hierarchy(spec: Specification, children: ET.Element | None, level: int, parent: int | None, prefix: str) -> None:
    """The SPEC-HIERARCHY below `children` as entries of `spec`, depth first: the order of the module."""
    for n, h in enumerate(_children(children, "SPEC-HIERARCHY"), 1):
        spec.entries.append(Entry(_ref(h, "OBJECT"), level, len(spec.entries) + 1, f"{prefix}{n}", parent))
        _hierarchy(spec, _child(h, "CHILDREN"), level + 1, len(spec.entries) - 1, f"{prefix}{n}.")


def _value(v: ET.Element, enum_names: dict[str, str]) -> str:
    kind = _tag(v)
    if kind == "ATTRIBUTE-VALUE-XHTML":
        the_value = _child(v, "THE-VALUE")
        return xhtml_text(the_value) if the_value is not None else ""
    if kind == "ATTRIBUTE-VALUE-ENUMERATION":
        values = _child(v, "VALUES")
        return ", ".join(enum_names.get((r.text or "").strip(), (r.text or "").strip()) for r in _children(values))
    return v.get("THE-VALUE", "")


@dataclass
class ReqifReport(Report):
    MISSING = "objects that are in none of the files"
    KEY = "IDENTIFIER"

    out_of_pattern: list[str] = field(default_factory=list)


class Importer:
    def __init__(self, content: Content, mapping: Mapping):
        self.content, self.mapping = content, mapping
        self.everywhere = content.attribute_names()
        self.module_of: dict[str, Specification] = {}             # object -> the first specification with it
        for spec in content.specifications:
            for e in spec.entries:
                self.module_of.setdefault(e.object, spec)
        self._ids: dict[str, str] = {}

    def name(self, attribute: str) -> str:
        """The name of a mapped attribute in these files (see ALIASES)."""
        if attribute in self.everywhere:
            return attribute
        alias = ALIASES.get(attribute.lower())
        return alias if alias in self.everywhere else attribute

    def values(self, obj: SpecObject) -> dict[str, str]:
        """The values of an object by the names the mapping uses."""
        m = self.mapping
        names = {n for n in (m.text, m.heading, m.kind_attribute, *m.id, *m.attributes.values()) if n}
        return {n: obj.values[self.name(n)] for n in names if self.name(n) in obj.values}

    def id_of(self, identifier: str, sources: tuple[str, ...] = ()) -> str:
        obj = self.content.objects.get(identifier)
        if obj is None:
            return ""
        spec = self.module_of.get(identifier)
        number = obj.values.get(FOREIGN_ID, "")
        built_in = {"identifier": obj.identifier, "absolute_number": number,
                    "module_number": f"{self.mapping.module_name(spec.name)}-{number}" if spec and number else ""}
        for source in sources or self.mapping.id:
            if value := " ".join(built_in.get(source, obj.values.get(self.name(source), "")).split()):
                return value
        return ""

    def check(self, specs: list[Specification]) -> list[str]:
        """Raises MappingError for an attribute no object type has; warns per specification."""
        m = self.mapping
        wanted = {n for n in (m.kind_attribute, *m.attributes.values(), *(set(m.id) - BUILT_IN)) if n}
        missing = sorted(n for n in wanted if self.name(n) not in self.everywhere)
        if missing:
            hints = [f"{n!r} (closest: {', '.join(difflib.get_close_matches(n, sorted(self.everywhere), 3, 0.5)) or '-'})"
                     for n in missing]
            raise MappingError(f"no SPEC-OBJECT-TYPE of the files has the attributes {', '.join(hints)}")
        notes = [f"no SPEC-OBJECT-TYPE has {n!r}: the {what} are empty" for n, what in ((m.text, "texts"),
                                                                                      (m.heading, "headings"))
                 if self.name(n) not in self.everywhere]
        for spec in specs:
            names = self.content.attribute_names(spec)
            lacking = sorted(n for n in wanted if self.name(n) not in names)
            if lacking:
                notes.append(f"{spec.name}: has no {', '.join(lacking)}"
                             + ("; its kinds come from the content" if m.kind_attribute in lacking else ""))
        return notes

    def module(self, spec: Specification, id_pattern: re.Pattern | None = None) -> tuple[list[dict], list[dict], ReqifReport]:
        m = self.mapping
        report = ReqifReport(module=m.module_name(spec.name), path=spec.source)
        by_attribute = bool(m.kind_attribute) and self.name(m.kind_attribute) in self.content.attribute_names(spec)
        seen: dict[str, str] = {}
        published: dict[int, str] = {}                  # entry -> the ID it was published with
        requirements, links = [], []
        for i, entry in enumerate(spec.entries):
            obj = self.content.objects.get(entry.object)
            rid = self.id_of(entry.object) if obj else ""
            if not rid:
                report.without_id += 1
                continue
            if rid in seen:
                report.repeated_ids.append(f"{rid} ({seen[rid]}, {obj.identifier})")
                continue
            seen[rid], published[i] = obj.identifier, rid
            if id_pattern and not id_pattern.search(rid):
                report.out_of_pattern.append(rid)
            parent, up = None, entry.parent
            while up is not None and parent is None:            # the nearest ancestor with an ID
                parent, up = published.get(up), spec.entries[up].parent
            values = self.values(obj)
            kind = m.kind(values, by_attribute)
            report.kinds[kind] += 1
            if by_attribute and _value_key(value := values.get(m.kind_attribute, "")) not in m.kind_values:
                report.other_values[" ".join(value.split())] += 1
            if kind == "requirement" and len(m.id) > 1 and not self.id_of(entry.object, m.id[:1]):
                report.other_ids.append(rid)
            defined = self.content.type_attributes.get(obj.type, set())
            requirements.append({
                "id": rid, "text": values.get(m.text, ""), "heading": values.get(m.heading, ""), "kind": kind,
                "parent": parent, "position": entry.position, "level": entry.level, "outline": entry.outline,
                "source_key": obj.identifier,
                "attributes": {name: obj.values.get(self.name(source), "") for name, source in m.attributes.items()
                               if self.name(source) in defined}})
        mine = {spec.entries[i].object: rid for i, rid in published.items()}
        for source, target, kind in self.content.relations:
            if source not in mine:
                continue
            target_id = self.id_of(target) if target in self.module_of else ""
            if not target_id:
                report.unresolved_links += 1
                if target not in self.content.objects or target not in self.module_of:
                    report.not_downloaded[target] += 1
                continue
            links.append({"source": mine[source], "target": target_id, "type": m.link_type(kind),
                          "target_module": m.module_name(self.module_of[target].name)})
        report.requirements, report.links = len(requirements), len({(l["source"], l["target"], l["type"]) for l in links})
        return requirements, links, report


def import_reqif(files: list[str | Path], mapping: Mapping, out: str | Path, modules: list[str] | None = None,
                 keep_others: bool = False, id_pattern: str = "") -> list[ReqifReport]:
    """Imports every specification of the files (or the given ones, by their ReqIF or atlas name)."""
    content = read_reqif(files)
    specs = [s for s in content.specifications
             if not modules or s.name in modules or mapping.module_name(s.name) in modules]
    for missing in sorted(set(modules or []) - {s.name for s in specs} - {mapping.module_name(s.name) for s in specs}):
        log.warning("%s: no SPECIFICATION of that name in the files; not imported", missing)
    importer = Importer(content, mapping)
    for note in importer.check(specs):
        log.warning("%s", note)
    pattern = re.compile(id_pattern) if id_pattern else None
    reports = []
    with ExchangeWriter(out, filler="tools/reqif_import") as writer:
        if not keep_others and not modules:
            for old in sorted(set(writer.modules()) - {mapping.module_name(s.name) for s in specs}):
                log.info("%s: no longer imported, removed from %s", old, out)
                writer.remove_module(old)
        for spec in specs:
            requirements, links, report = importer.module(spec, pattern)
            warn(report, mapping)
            if report.out_of_pattern:
                log.warning("%s: %d IDs do not match %s (atlas reports them as outside the convention): %s",
                            report.module, len(report.out_of_pattern), id_pattern, _listed(report.out_of_pattern))
            write(writer, report, requirements, links)
            reports.append(report)
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fills the exchange database of atlas from ReqIF files.")
    parser.add_argument("files", nargs="+", help=".reqif or .reqifz files, read together")
    parser.add_argument("--mapping", help="mapping file (see tools/doors_export/mapping.example.toml)")
    parser.add_argument("--out", default="requirements.sqlite", help="exchange database to fill")
    parser.add_argument("--module", action="append", default=[], metavar="NAME",
                        help="import only this specification (its ReqIF or atlas name); can be repeated")
    parser.add_argument("--keep-others", action="store_true",
                        help="do not remove the modules of the exchange database that are not imported now")
    parser.add_argument("--id-pattern", default="", metavar="REGEX",
                        help="warn about the IDs that do not match it ([sources.doors.convention] id_pattern)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        mapping = Mapping.load(args.mapping) if args.mapping else Mapping()
        if not args.mapping:
            log.warning("without --mapping, the ID of each requirement is the IDENTIFIER of its SPEC-OBJECT")
        reports = import_reqif(args.files, mapping, args.out, args.module, args.keep_others, args.id_pattern)
    except (MappingError, ExchangeError, ValueError, OSError) as e:
        log.error("%s", e)
        return 1
    if not reports:
        log.error("no SPECIFICATION to import in %s", ", ".join(args.files))
        return 1
    print(f"\nExchange database: {args.out}")
    for r in reports:
        print(f"  {r.summary()}")
    return 1 if any(r.error for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
