"""The quantities of a requirement text (deadlines, limits, rates, sizes), to compare the
text of DOORS with the one of a document without a model.

    quantities("within 300 ms, up to 1,000 tracks")   # [(300.0, "ms"), (1000.0, "")]
    values_differ("within 320 ms", "within 300 ms")    # {"doors": ["320 ms"], "document": ["300 ms"],
                                                       #  "kind": "value"}

What counts as a quantity:
- a number, with the unit that follows it if it is one of UNITS ("ms", "milliseconds",
  "segundos"...: each one has a canonical unit), or a number word ("two", "dos");
- a comma or a point followed by exactly three digits is a thousands separator ("1,000" and
  "1.000" are 1000, unless the number starts with 0: "0,125"); any other is the decimal
  separator ("1,5" is 1.5); a number with more groups is a thousands number ("1,000,000",
  "1,000.5") or, if it is not, the number of a section ("1.1.1"), which is left out;
- references are left out: "section 3.2", "Table 3", "figura 4", "page 12", "anexo B.2",
  dates ("2026-10-14", "14/10/2026"), versions ("v2", "v1.3") and requirement IDs
  ("SRS-0001").

Two quantities are the same when they have the same value and the same canonical unit
(1.5 and 1.50 are the same). A unit is never converted: 300 ms and 0.3 s are the same
time, but saying so is the job of the reader (or the model), not of a table.

values_differ(official, document) says how the quantities of the two texts differ:
- "value": a unit (or no unit) has a value in one text and another value in the other:
  320 ms against 300 ms, 30 days against 60 days. The one that matters for a review;
- "unit_only": the different quantities have different units of the same magnitude
  (300 ms against 0.3 s), or the same value with a unit in only one text;
- "format_only": the same quantities written in another way ("1,000" and "1000", "1.5" and
  "1.50", "two" and "2", "ms" and "milliseconds").
A quantity that only one text has, with nothing to compare it with in the other, is not a
difference: the document may say more than the requirement.
"""
from __future__ import annotations

import re

__all__ = ["UNITS", "quantities", "values_differ"]

# canonical unit -> how it is written (compared in any case, except the symbols of CASED)
UNITS = {
    "ms": ("ms", "msec", "msecs", "millisecond", "milliseconds", "milisegundo", "milisegundos"),
    "s": ("s", "sec", "secs", "second", "seconds", "seg", "segundo", "segundos"),
    "min": ("min", "mins", "minute", "minutes", "minuto", "minutos"),
    "h": ("h", "hr", "hrs", "hour", "hours", "hora", "horas"),
    "m": ("m", "metre", "metres", "meter", "meters", "metro", "metros"),
    "km": ("km", "kilometre", "kilometres", "kilometer", "kilometers", "kilómetro", "kilómetros"),
    "Hz": ("hz", "hertz", "hercio", "hercios"),
    "kHz": ("khz", "kilohertz", "kilohercio", "kilohercios"),
    "%": ("%", "percent", "per cent", "por ciento"),
    "B": ("B", "byte", "bytes"),
    "KB": ("kb", "kib", "kilobyte", "kilobytes"),
    "MB": ("mb", "mib", "megabyte", "megabytes"),
}
CASED = {"m", "B"}                 # "M" and "b" (mega, bit) are not these
DIMENSION = {"ms": "time", "s": "time", "min": "time", "h": "time", "m": "length", "km": "length",
             "Hz": "frequency", "kHz": "frequency", "B": "size", "KB": "size", "MB": "size", "%": "ratio"}
WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "cero": 0, "uno": 1, "una": 1, "dos": 2, "tres": 3,
         "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12}

_SPELLING = {w if canonical in CASED and w == canonical else w.lower(): canonical
             for canonical, written in UNITS.items() for w in written}
REFERENCE = re.compile(
    r"(?i)(?:\b(?:sections?|secci[oó]n(?:es)?|tables?|tablas?|figures?|figuras?|fig\.|pages?|p[aá]ginas?|pp?\.|"
    r"chapters?|cap[ií]tulos?|annex(?:es)?|anexos?|appendix|ap[eé]ndices?|items?|paragraphs?|p[aá]rrafos?|"
    r"versions?|versi[oó]n(?:es)?|rev\.?|issue|edici[oó]n|edition)|§)\s*(?:[A-Z](?=[.\d]))?[.\-]?\d+(?:[.\-]\d+)*")
DATE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
VERSION = re.compile(r"(?i)\bv\d+(?:\.\d+)*\b")
ID_LIKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)+\b")      # SRS-0001: not a value
QUANTITY = re.compile(
    r"(?<![\w.,])(?:(?P<num>\d+(?:[.,]\d+)*)|(?P<word>\b(?:" + "|".join(WORDS) + r")\b))"
    r"(?:\s?(?P<unit>%|per cent|por ciento|[^\W\d_]+\b))?", re.IGNORECASE)


def _number(text: str) -> float | None:
    """The value of a written number by the rules of the module docstring; None for the
    number of a section ("1.1.1")."""
    groups = re.split(r"[.,]", text)
    seps = re.findall(r"[.,]", text)
    if not seps:
        return float(text)
    thousands = groups[0] != "0" and all(len(g) == 3 for g in groups[1:])
    if len(groups) == 2:
        return float(text.replace(text[len(groups[0])], "")) if thousands else float(f"{groups[0]}.{groups[1]}")
    if thousands and len(set(seps)) == 1:                            # 1,000,000
        return float("".join(groups))
    if len(set(seps)) == 2 and seps[-1] not in seps[:-1] and groups[0] != "0" \
            and all(len(g) == 3 for g in groups[1:-1]):              # 1,000.5 and 1.000,5
        return float("".join(groups[:-1]) + "." + groups[-1])
    return None


def _unit(written: str | None) -> str:
    """The canonical unit of a word after a number; "" if it is not a unit ("tracks")."""
    if not written:
        return ""
    if written in _SPELLING:                 # as written: "m", "B", "ms"
        return _SPELLING[written]
    low = written.lower()
    return "" if low in {c.lower() for c in CASED} else _SPELLING.get(low, "")     # "M", "b": not these


def _found(text: str) -> list[tuple[float, str, str]]:
    """(value, canonical unit, as written) of every quantity of `text`, in order."""
    clean = text or ""
    for pattern in (REFERENCE, DATE, VERSION, ID_LIKE):
        clean = pattern.sub(" ", clean)
    out = []
    for m in QUANTITY.finditer(clean):
        value = float(WORDS[m["word"].lower()]) if m["word"] else _number(m["num"])
        if value is None:
            continue
        unit = _unit(m["unit"])
        written = (m["word"] or m["num"]) + (f" {m['unit']}" if unit else "")
        out.append((value, unit, written))
    return out


def quantities(text: str) -> list[tuple[float, str]]:
    """The quantities of a text: (value, canonical unit or "") in order of appearance."""
    return [(v, u) for v, u, _ in _found(text)]


def _key(item: tuple[float, str, str]) -> str:
    """How a quantity is written, without spaces and in lower case: "1,000" and "1000" are
    two ways, "95 %" and "95%" the same one."""
    return "".join(item[2].split()).lower()


def values_differ(official: str, document: str) -> dict | None:
    """How the quantities of the official text (DOORS) and of a document differ:
    {"doors": [...], "document": [...], "kind": "value" | "unit_only" | "format_only"},
    each list with the quantities as written in its text; None if they do not differ (see
    the module docstring)."""
    a, b = _found(official), _found(document)
    sa, sb = {(v, u) for v, u, _ in a}, {(v, u) for v, u, _ in b}

    def unique(items):
        seen, out = set(), []
        for x in items:
            if x[2] not in seen:
                seen.add(x[2])
                out.append(x[2])
        return out

    if sa == sb:
        ka, kb = {_key(x) for x in a}, {_key(x) for x in b}
        if ka == kb:
            return None
        return {"doors": unique(x for x in a if _key(x) not in kb),
                "document": unique(x for x in b if _key(x) not in ka), "kind": "format_only"}
    only_a = [x for x in a if (x[0], x[1]) not in sb]
    only_b = [x for x in b if (x[0], x[1]) not in sa]
    units_a, units_b = {x[1] for x in only_a}, {x[1] for x in only_b}
    if units_a & units_b:
        kind = "value"
    elif any(DIMENSION.get(x[1]) and DIMENSION.get(x[1]) == DIMENSION.get(y[1]) for x in only_a for y in only_b) or \
            any(x[0] == y[0] and "" in (x[1], y[1]) for x in only_a for y in only_b):
        kind = "unit_only"
    else:
        return None                     # nothing to compare: one text says more than the other
    return {"doors": unique(only_a), "document": unique(only_b), "kind": kind}
