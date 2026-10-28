"""Test procedures as code, without a model: each numbered step of a test procedure of DOORS
becomes a line of a test in each language, by the patterns and templates of the project.

    atlas tests STP_SOCKET atlas.toml        (or one procedure: atlas tests STP-0101)

A test written by a model is not always the procedure: gemma-4-26b and devstral, asked to fix
a test that failed, changed the responses it expected or the end of its lines so that it
passed. A step written as a pattern is the same in every test and every language, and the
procedure is the only place to change it. [tests] in atlas.toml:

    [tests.languages.ada]                    # a test per procedure and language
    path = "tests/ada/test_{slug}.adb"       # {id} STP-0101, {slug} stp_0101, {Slug} Stp_0101
    template = "requirements/templates/test.adb"
    [tests.languages.python]
    path = "tests/python/test_{slug}.py"
    template = "requirements/templates/test.py"

    [[tests.steps]]                          # the first pattern that matches a step gives its code
    match = 'Send `(?P<command>[^`]*)`; expect `(?P<expected>[^`]*)`'
    ada = 'Expect ({n}, {command:str}, {expected:str});'
    python = 'client.expect({n}, {command:str}, {expected:str})'

`match` is a regular expression for the whole text of a step, without its final period. In the
code of a step, {n} is its number, {text} its text and {name} a named group of `match`, as it
is; {name:str} is the same value as a string literal of the language ("..." with "" inside for
Ada; "..." with backslashes for the others, as JSON writes it). A template is the whole file,
with {id}, {slug}, {Slug}, {title} (the heading in DOORS), {verifies} (the IDs the procedure
verifies) and {steps} alone in its line, where the code of the steps goes with the indentation
of that line; other braces are left as they are.

The steps are "1. ...", "2. ..." in the text of the procedure, in order, with or without line
breaks between them (the store joins the lines of a text). A procedure with a step that no
pattern matches is not written, and the step is reported: a new kind of step needs a pattern,
or the step is written like the others. A file is only written when its content changes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["generate_tests", "literal", "split_steps", "slugs"]

PLACEHOLDER = re.compile(r"\{(\w+)(?::(str))?\}")


def split_steps(text: str) -> tuple[str, list[tuple[int, str]]]:
    """(what goes before the steps, [(number, text of the step)]): the steps 1, 2, 3...
    in this order; "1." inside a step ("VERSION 1.0") is not a step."""
    starts, at, n = [], 0, 1
    while m := re.compile(rf"(?:^|(?<=\s)){n}[.)]\s+").search(text, at):
        starts.append((n, m.start(), m.end()))
        at, n = m.end(), n + 1
    if not starts:
        return text.strip(), []
    steps = []
    for k, (n, start, end) in enumerate(starts):
        stop = starts[k + 1][1] if k + 1 < len(starts) else len(text)
        body = text[end:stop]
        if k + 1 == len(starts):                    # the last one ends at a blank line, if any
            body = re.split(r"\n\s*\n", body, maxsplit=1)[0]
        steps.append((n, re.sub(r"\s*\n\s*", " ", body).strip()))      # spaces inside a line, as they are
    return text[:starts[0][1]].strip(), steps


def literal(value: str, language: str) -> str:
    """`value` as a string literal of the language."""
    if language.lower() == "ada":
        return '"' + value.replace('"', '""') + '"'
    return json.dumps(value, ensure_ascii=False)


def slugs(req_id: str) -> dict:
    slug = re.sub(r"\W+", "_", req_id).strip("_").lower()
    return {"slug": slug, "Slug": "_".join(w[:1].upper() + w[1:] for w in slug.split("_"))}


def _fill(template: str, values: dict, language: str, strict: bool) -> str:
    def sub(m: re.Match) -> str:
        name, as_str = m[1], m[2]
        if name not in values:
            if strict:
                raise KeyError(name)
            return m[0]                             # braces of the code
        value = "" if values[name] is None else str(values[name])
        return literal(value, language) if as_str else value
    return PLACEHOLDER.sub(sub, template)


def _step_code(step: tuple[int, str], patterns: list[dict], language: str) -> str:
    """The code of a step in a language; ValueError with the reason when there is none."""
    n, text = step
    text = text.strip()
    text = text[:-1].rstrip() if text.endswith(".") else text
    for i, pattern in enumerate(patterns):
        m = re.fullmatch(pattern["match"], text)
        if not m:
            continue
        if language not in pattern:
            raise ValueError(f"step {n} matches [[tests.steps]] {i + 1}, which has no code for {language}")
        try:
            return _fill(pattern[language], {**m.groupdict(), "n": n, "text": text}, language, strict=True)
        except KeyError as e:
            raise ValueError(f"[[tests.steps]] {i + 1}, {language}: {{{e.args[0]}}} is not n, text or a named "
                             f"group of its match") from None
    raise ValueError(f"no pattern of [[tests.steps]] matches step {n}: {text!r}")


def _file(template: str, values: dict, code: list[str], language: str) -> str:
    out = []
    for line in template.splitlines(keepends=True):
        if "{steps}" in line:
            indent = line[:line.index("{steps}")]
            out += [f"{indent}{l}\n" if l.strip() else "\n" for block in code for l in block.split("\n")]
        else:
            out.append(_fill(line, values, language, strict=False))
    return "".join(out)


def generate_tests(graph, target: str, settings: dict, base: str | Path = ".") -> dict:
    """Writes the tests of a test procedure (an ID) or of those of a module, with the
    [tests] `settings`. Returns {"procedures": [{"id", "files": [{"path", "status"}],
    "errors": [...]}], "written", "unchanged", "errors"}, or {"error"}."""
    languages, patterns = settings.get("languages") or {}, settings.get("steps") or []
    if not languages:
        return {"error": "no languages in [tests] of atlas.toml: nothing to write"}
    base = Path(base)
    templates = {}
    for language, spec in languages.items():
        path = base / spec["template"]
        if not path.is_file():
            return {"error": f"[tests.languages.{language}] template {spec['template']} does not exist"}
        templates[language] = path.read_text(encoding="utf-8")
    nodes = graph.G.nodes
    if target in nodes and nodes[target].get("kind") == "requirement":
        ids = [target]
    else:
        found = graph.list_requirements("", target, 10**6)
        if "error" in found:
            return found
        ids = [item["id"] for item in found["items"]]
    out = {"procedures": [], "written": 0, "unchanged": 0, "errors": 0}
    for req_id in ids:
        node = nodes[req_id]
        _, steps = split_steps(node.get("text") or "")
        record = {"id": req_id, "files": [], "errors": []}
        out["procedures"].append(record)
        if not steps:
            record["errors"].append("it has no numbered steps (1. ..., 2. ...)")
            continue
        verifies = [link["id"] for link in graph.trace(req_id).get("links_out", []) if link["type"] == "verifies"]
        values = {"id": req_id, "title": node.get("heading", ""), "verifies": ", ".join(verifies), **slugs(req_id)}
        files = {}
        for language, spec in languages.items():
            try:
                code = [_step_code(step, patterns, language) for step in steps]
            except ValueError as e:
                record["errors"].append(f"{language}: {e}")
                continue
            files[spec["path"]] = _file(templates[language], values, code, language)
        if record["errors"]:                        # all or nothing: the languages say the same
            continue
        for path, content in files.items():
            target_file = base / _fill(path, values, "", strict=False)
            same = target_file.is_file() and target_file.read_text(encoding="utf-8") == content
            if not same:
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(content, encoding="utf-8")
            record["files"].append({"path": _fill(path, values, "", strict=False),
                                    "status": "unchanged" if same else "written"})
            out["unchanged" if same else "written"] += 1
    out["errors"] = sum(bool(p["errors"]) for p in out["procedures"])
    return out
