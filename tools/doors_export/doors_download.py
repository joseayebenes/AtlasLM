"""Download of DOORS Classic modules to the raw copy in SQLite (raw_store.py).

Second file of the download. It talks to DOORS through the Automation interface (COM/OLE),
runs **read-only** DXL and keeps what it reads with raw_store.py:

    # one module
    python tools/doors_export/doors_download.py --module "/Project/Requirements/SRS"

    # every formal module under a folder or project
    python tools/doors_export/doors_download.py --folder "/Project" --db doors_raw.sqlite

    # before the first download: do the DXL primitives behave as expected?
    python tools/doors_export/doors_download.py --check --module "/Project/Requirements/SRS"

It needs Windows, DOORS Classic and `pip install pywin32`; Python and DOORS must run as the
same Windows user, because Automation does not cross sessions. Then publish.py turns the
raw copy into the exchange database of atlas (no DOORS needed for that).

Comes from scripts/descargar_requisitos.py of the doors_kb project; besides the attributes
it now reads the hierarchy (parent and level) and the outgoing links of every object. The
four rules of the original, born of real failures against DOORS, still hold:

1. **Own session.** Created with Dispatch, never GetActiveObject: a DOORS window opened by
   hand does not show up reliably in the Running Object Table.
2. **Explicit module.** Every script opens the module by its fullName with read(...).
3. **One COM thread, with an external timeout.** The Automation object lives in the
   apartment of the thread that created it, and a modal dialog in DOORS would hang forever.
4. **Not one quote in the answer.** DXL scripts do not build JSON: they emit every value
   preceded by its length (`15:Absolute Number`) and Python reads it. No three layers of
   escaping (Python, DXL, JSON) to get wrong.

The walk is by cursor: every page goes on from object(absno, m) with next(o), instead of
walking the module from the start and skipping objects (linear instead of quadratic cost).
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import secrets
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from raw_store import DoorsObject, Link, RawStore, Stats  # noqa: E402

log = logging.getLogger("doors-download")

PROTOCOL = "DKB1"      # mark at the start of every answer: tells it from a loose DXL message


class DoorsError(RuntimeError):
    """Any failure talking to DOORS: session, module or DXL run."""


# -------------------------------------------------------------------------------------------
# DXL generation
# -------------------------------------------------------------------------------------------

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def escape(value: str) -> str:
    """Escapes a value for a DXL string literal: the only door through which Python data
    gets into a script (a module path with quotes would otherwise close the literal). Control
    characters DXL cannot represent are dropped."""
    return "".join(_ESCAPES.get(c, c) for c in value if c in _ESCAPES or ord(c) >= 0x20)


# Emits a value with its length in front: no quotes, braces or escapes. A value can hold
# anything (quotes, backslashes, line breaks), because Python counts characters.
_HELPERS = """
// The order is `v ""`, with the number first; the reverse form does not compile.
string asText(int v) {
    return v ""
}

string ns(string s) {
    // The length goes to a variable before converting it: `length(s) ""` is parsed as
    // `length(s "")`, the expression returns an integer and the assignment fails.
    int n = length(s)
    string header = asText(n)
    return header ":" s
}

string nsInt(int v) {
    return ns(asText(v))
}

string nsBool(bool v) {
    if (v) { return ns("1") }
    return ns("0")
}

// Cuts leaving room for the mark, so that the result never goes over maxChars: without the
// mark a short text cannot be told from a cut one.
string cut(string s, int maxChars) {
    if (maxChars <= 0) { return s }
    if (length(s) <= maxChars) { return s }
    int mark = 14
    if (maxChars <= mark) { return s[0 : maxChars - 1] }
    return s[0 : maxChars - mark - 1] "... [truncated]"
}
"""


def _preamble(run_limit: int = 0) -> str:
    """The internal DXL watchdog: `pragma runLim, 0` turns it off and leaves the control to
    the Python timeout (the watchdog cannot tell a hung script from a large module). It ends
    in a **real** line break: otherwise the pragma and the first statement share a line and
    DXL fails with a syntax error that does not mention the pragma."""
    return f"pragma runLim, {int(run_limit)}\n"


def _header(buffer: str, token: str, kind: str) -> str:
    """Common header: mark, token of the call and kind. The token matters: when a DXL script
    fails, oleSetResult never runs and DOORS keeps the result of the previous call; without
    the token Python would take that stale answer for the new one."""
    if not token.isalnum():
        raise ValueError(f"the token must be alphanumeric: {token!r}")
    return f'{buffer} += ns("{PROTOCOL}")\n{buffer} += ns("{token}")\n{buffer} += ns("{kind}")\n'


def _open_module(module_path: str, token: str) -> str:
    """Opens the module read-only and stops with an explicit error if it cannot."""
    return f"""
Module m = read("{escape(module_path)}", false)
if (null m) {{
    Buffer e = create
{_header("e", token, "ERROR")}    e += ns("NO_MODULE")
    oleSetResult(stringOf e)
    delete e
    halt
}}
"""


def script_modules(folder: str, token: str, run_limit: int = 0) -> str:
    """Every formal module under a folder or project, recursively (link and descriptive
    modules hold no requirements)."""
    return _preamble(run_limit) + _HELPERS + f"""
Buffer data = create
int total = 0

void walk(Folder f) {{
    if (null f) {{ return }}
    Item it
    for it in f do {{
        string t = type(it)
        if (t == "Formal") {{
            data += ns(fullName(it))
            total++
        }} else if (t == "Folder" || t == "Project") {{
            walk(folder(it))
        }}
    }}
}}

Folder root = folder("{escape(folder)}")
if (null root) {{
    Buffer e = create
{_header("e", token, "ERROR")}    e += ns("NO_FOLDER")
    oleSetResult(stringOf e)
    delete e
    delete data
    halt
}}
walk(root)

Buffer b = create
{_header("b", token, "MODULES")}b += nsInt(total)
b += stringOf data
oleSetResult(stringOf b)
delete data
delete b
"""


def script_attributes(module_path: str, token: str, run_limit: int = 0) -> str:
    """The object attributes of the module (name, type, system or not). Asked before reading
    anything: to download *every* attribute without the user listing them, and because
    reading a missing attribute gives an empty string, like an empty one."""
    return _preamble(run_limit) + _HELPERS + _open_module(module_path, token) + f"""
Buffer b = create
{_header("b", token, "ATTRS")}
int total = 0
AttrDef adc
for adc in m do {{
    if (adc.object) {{ total++ }}
}}
b += nsInt(total)

AttrDef ad
for ad in m do {{
    if (!ad.object) {{ continue }}
    AttrType at = ad.type
    b += ns(ad.name)
    b += ns(at.name "")
    b += nsBool(ad.system)
}}
oleSetResult(stringOf b)
delete b
"""


# Outgoing links of the object `o`: target module and Absolute Number (both without opening
# the target module) and the link module. Not validated against DOORS when written: run
# --check first.
_LINKS = """    setempty(linkData)
    int nLinks = 0
    Link l
    for l in o -> "*" do {{
        ModName_ targetMod = target(l)
        int targetAbs = targetAbsNo(l)
        linkData += ns(fullName(targetMod))
        linkData += nsInt(targetAbs)
        {link_module}
        nLinks++
    }}
    data += nsInt(nLinks)
    data += stringOf linkData
"""
_LINK_MODULE = "ModName_ linkMod = module(l)\n        linkData += ns(fullName(linkMod))"


def script_page(module_path: str, attributes: Sequence[str], token: str, *, cursor: int | None = None,
                page_size: int = 100, max_attribute_chars: int = 20_000, links: bool = True,
                link_module: bool = True, run_limit: int = 0) -> str:
    """One page of objects from the cursor.

    Emits PAGE, the next cursor (empty at the end of the module) and, for every object: its
    Absolute Number, identifier, outline number, the Absolute Number of its parent (empty at
    the top), its level, the requested attributes in order and, with `links`, its outgoing
    links. Attribute names do not travel: the order is the contract with the reader.

    An empty next cursor is the only sign that the module was walked whole, and marking the
    missing ones as deleted depends on it; that is why the objects go to a separate buffer:
    the cursor is not known until the end, and it goes before the data."""
    if cursor is None:
        position = "Object o = first(m)\n"
    else:
        # object(absno, m) jumps straight to the object. If the cursor object no longer
        # exists (deleted between two pages), the walk starts again from the beginning: it
        # costs more, but does not stop the download.
        position = f"""
Object o = null
Object cursorObj = object({int(cursor)}, m)
if (null cursorObj) {{
    o = first(m)
    while (!null o && (int)(o."Absolute Number") <= {int(cursor)}) {{ o = next(o) }}
}} else {{
    o = next(cursorObj)
}}
"""
    reads = "".join(f'    data += ns(cut(o."{escape(name)}" "", {int(max_attribute_chars)}))\n' for name in attributes)
    link_block = _LINKS.format(link_module=_LINK_MODULE if link_module else 'linkData += ns("")') if links else ""
    return _preamble(run_limit) + _HELPERS + _open_module(module_path, token) + position + f"""
Buffer b = create
Buffer data = create
Buffer linkData = create
int emitted = 0
int last = -1
bool exhausted = true
while (!null o) {{
    if (emitted >= {int(page_size)}) {{ exhausted = false; break }}
    // Deleted objects and the inner cells of tables are not requirements.
    if (isDeleted(o) || table(o) || row(o) || cell(o)) {{ o = next(o); continue }}
    last = (int)(o."Absolute Number")
    data += nsInt(last)
    data += ns(identifier(o))
    data += ns(number(o))
    Object up = parent(o)
    if (null up) {{ data += ns("") }} else {{ int upAbs = (int)(up."Absolute Number"); data += nsInt(upAbs) }}
    int lv = level(o)
    data += nsInt(lv)
{reads}{link_block}    emitted++
    o = next(o)
}}
{_header("b", token, "PAGE")}if (exhausted) {{ b += ns("") }} else {{ b += nsInt(last) }}
b += nsInt(emitted)
b += stringOf data
oleSetResult(stringOf b)
delete linkData
delete data
delete b
"""


# Primitives that the hierarchy and the links need, checked one by one by --check: each
# leaves its result in `value`. The rest of the DXL was checked against DOORS by doors_kb.
# The link probes run `body` on the first outgoing link of the module.
_LINK_PROBE = """Object o = first(m)
string value = "no object with outgoing links"
bool found = false
while (!null o && !found) {{
    Link l
    for l in o -> "*" do {{
        if (!found) {{
            {body}
            found = true
        }}
    }}
    o = next(o)
}}
"""
CHECKS: tuple[tuple[str, str, str], ...] = (
    ("parent(o)", "Object o = first(m)\nObject c = o\nwhile (!null c && null parent(c)) { c = next(c) }\n"
                  'string value = (null c) ? "no object with a parent" : identifier(parent(c))', "the hierarchy"),
    ("level(o)", 'Object o = first(m)\nint lv = level(o)\nstring value = lv ""', "the hierarchy"),
    ("ModName_ target(l) and fullName",
     _LINK_PROBE.format(body="ModName_ t = target(l)\n            value = fullName(t)"), "the links (or --no-links)"),
    ("targetAbsNo(l)", _LINK_PROBE.format(body='int a = targetAbsNo(l)\n            value = a ""'),
     "the links (or --no-links)"),
    ("ModName_ module(l) (link module)",
     _LINK_PROBE.format(body="ModName_ lm = module(l)\n            value = fullName(lm)"),
     "the link module (or --no-link-module)"),
)


def script_check(module_path: str, body: str, token: str) -> str:
    return (_preamble() + _HELPERS + _open_module(module_path, token)
            + f"{body}\nBuffer b = create\n{_header('b', token, 'CHECK')}b += ns(value)\noleSetResult(stringOf b)\n"
              "delete b\n")


# -------------------------------------------------------------------------------------------
# Reading the answer
# -------------------------------------------------------------------------------------------

class Reader:
    """Goes through the fields of an answer in the order the script emitted them."""

    def __init__(self, fields: list[str]) -> None:
        self._fields = fields
        self._pos = 0

    def text(self) -> str:
        if self._pos >= len(self._fields):
            raise DoorsError("the answer of DOORS ended before expected: fields are missing. It usually came cut; "
                             "try a smaller --page.")
        value = self._fields[self._pos]
        self._pos += 1
        return value

    def integer(self) -> int:
        raw = self.text()
        try:
            return int(raw)
        except ValueError as e:
            raise DoorsError(f"a number was expected from DOORS and {raw[:80]!r} came") from e

    def boolean(self) -> bool:
        return self.text() == "1"

    def optional_integer(self) -> int | None:
        """A number that may come empty, like the cursor at the end of the module."""
        raw = self.text()
        return int(raw) if raw else None


def _split(raw: str) -> list[str]:
    """The answer in fields, checking that it is whole. Never a partial result: an
    incomplete list taken for complete would cause wrong logical deletions downstream."""
    if not raw:
        raise DoorsError("DOORS gave an empty answer. Check that the Automation session is alive and that the DXL "
                         "script was not interrupted.")
    if ":" not in raw[:40]:
        raise DoorsError(f"DOORS did not give a protocol answer but a message of the DXL interpreter "
                         f"({len(raw)} characters): {raw[:2000]!r}")
    fields, pos, total = [], 0, len(raw)
    while pos < total:
        sep = raw.find(":", pos)
        if sep < 0:
            raise DoorsError(f"malformed answer at {pos}: the length of a field was expected. "
                             f"Fragment: {raw[pos:pos + 120]!r}")
        try:
            length = int(raw[pos:sep])
        except ValueError as e:
            # the whole answer goes in the message: when the script fails, what arrives here
            # is the message of the DXL interpreter, the only thing that locates the error
            raise DoorsError(f"malformed answer at {pos}: {raw[pos:sep][:80]!r} is not the length of a field. "
                             f"Whole answer ({len(raw)} characters): {raw[:2000]!r}") from e
        start, end = sep + 1, sep + 1 + length
        if end > total:
            raise DoorsError(f"the answer of DOORS came cut: a field declares {length} characters and only "
                             f"{total - start} remain. Lower --page or --max-attribute-chars.")
        fields.append(raw[start:end])
        pos = end
    return fields


def parse(raw: str, token: str) -> tuple[str, Reader]:
    """(kind, reader), after checking that the answer is the one of **this** call."""
    reader = Reader(_split(raw))
    if reader.text() != PROTOCOL:
        raise DoorsError(f"the answer does not start with the protocol mark ({PROTOCOL}); it is usually a message "
                         f"of the DXL interpreter: {raw[:2000]!r}")
    received = reader.text()
    if received != token:
        raise DoorsError(f"DOORS gave the result of a previous call, not of this one (token {token!r} expected, "
                         f"{received!r} received): the DXL script did not finish. Open the 'DXL output' window "
                         f"in DOORS to see the error of the interpreter.")
    return reader.text(), reader


def read_page(reader: Reader, module_path: str, attributes: Sequence[str],
              links: bool = True) -> tuple[list[DoorsObject], int | None]:
    """The objects of a PAGE answer and the next cursor (None at the end of the module)."""
    next_cursor = reader.optional_integer()
    objects = []
    for _ in range(reader.integer()):
        absno, identifier, outline = reader.integer(), reader.text(), reader.text()
        parent, level = reader.optional_integer(), reader.integer()
        values = {name: reader.text() for name in attributes}
        found = tuple(Link(reader.text(), reader.integer(), reader.text()) for _ in range(reader.integer())) \
            if links else ()
        objects.append(DoorsObject(module_path=module_path, absolute_number=absno, identifier=identifier,
                                   outline_number=outline, parent_absolute_number=parent, level=level,
                                   attributes=values, links=found))
    return objects, next_cursor


# -------------------------------------------------------------------------------------------
# Session with DOORS
# -------------------------------------------------------------------------------------------

class DoorsSession:
    """Read-only Automation session, serialised in one COM thread.

    Every call is queued to a dedicated thread that does CoInitialize once, and every call
    has a timeout. After a timeout the session cannot be used any more: whether the DXL
    script still runs inside DOORS is unknown, and reusing it could mix the answer of one
    call with the next."""

    def __init__(self, *, prog_id: str = "DOORS.Application", start_timeout: int = 60, dxl_timeout: int = 300,
                 run_limit: int = 0) -> None:
        self.prog_id, self.start_timeout, self.dxl_timeout, self.run_limit = \
            prog_id, start_timeout, dxl_timeout, run_limit
        self._doors: Any = None
        self._queue: queue.Queue[tuple[Callable[[], Any], Future] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._blocked = ""

    @property
    def blocked(self) -> bool:
        """The session became unusable after a timeout."""
        return bool(self._blocked)

    # --- COM thread ---------------------------------------------------------------------
    def _loop(self) -> None:
        try:
            import pythoncom  # type: ignore[import-not-found]
            pythoncom.CoInitialize()
        except ImportError:  # pragma: no cover - only outside Windows
            pythoncom = None
        try:
            while True:
                task = self._queue.get()
                if task is None:
                    return
                function, future = task
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(function())
                except BaseException as e:   # passed intact to whoever waits
                    future.set_exception(e)
        finally:
            if pythoncom is not None:  # pragma: no cover
                pythoncom.CoUninitialize()

    def _call(self, function: Callable[[], Any], *, timeout: float) -> Any:
        if self._blocked:
            raise DoorsError(f"the session with DOORS is blocked ({self._blocked}). Restart the process before "
                             "downloading again.")
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="doors-com", daemon=True)
            self._thread.start()
        future: Future = Future()
        self._queue.put((function, future))
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as e:
            # The running call cannot be cancelled (DXL keeps running inside DOORS): the
            # only honest thing is to stop using this session.
            self._blocked = f"timeout of {timeout} s"
            raise DoorsError(f"the call to DOORS went over the timeout of {timeout} seconds. The DXL script may "
                             "still run inside DOORS, so the session cannot be used: restart the process. For a "
                             "large module, lower --page or raise --dxl-timeout.") from e

    # --- life cycle ---------------------------------------------------------------------
    def open(self) -> None:
        """Creates the session and waits for it to answer DXL. DOORS may show the login
        window: the user completes it by hand, hence a wait with a timeout."""
        if self._doors is not None:
            return
        log.info("Creating the Automation session with ProgID '%s'", self.prog_id)
        self._doors = self._call(self._create, timeout=self.start_timeout)
        limit, told = time.monotonic() + self.start_timeout, False
        while time.monotonic() < limit:
            if self._answers():
                log.info("DOORS session ready.")
                return
            if not told:
                log.info("Waiting for DOORS. If the login window appeared, complete it in the window opened by "
                         "Python.")
                told = True
            time.sleep(1.0)
        raise DoorsError(f"the DOORS session was not ready in {self.start_timeout} seconds. Complete the login in "
                         "the window opened by Python (not an older one) and try again, or raise --start-timeout.")

    def _create(self) -> Any:
        try:
            import win32com.client  # type: ignore[import-not-found]
        except ImportError as e:
            raise DoorsError("pywin32 is not installed. Access to DOORS Classic only works on Windows: install "
                             "pywin32 on the machine with DOORS.") from e
        try:
            return win32com.client.Dispatch(self.prog_id)
        except Exception as e:
            raise DoorsError(f"could not create the Automation session '{self.prog_id}': {e}. Check that DOORS "
                             "Classic is installed and that Python and DOORS run as the same Windows user.") from e

    def close(self) -> None:
        """Ends the COM thread. DOORS is not closed: the session is read-only."""
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=5.0)
            self._thread = None
        self._doors = None

    def __enter__(self) -> DoorsSession:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- running DXL --------------------------------------------------------------------
    @staticmethod
    def new_token() -> str:
        return secrets.token_hex(8)

    def _answers(self) -> bool:
        """Availability probe with a unique token (a fixed answer would be fooled by the
        stale result of a previous call)."""
        token = self.new_token()
        try:
            return self._dxl(f'oleSetResult("{token}")', timeout=self.start_timeout) == token
        except Exception:
            return False

    def _dxl(self, script: str, *, timeout: float | None = None) -> str:
        if self._doors is None:
            raise DoorsError("there is no session with DOORS: call open() first.")

        def run() -> str:
            self._doors.runStr(script)
            return str(self._doors.result or "")

        return self._call(run, timeout=timeout or self.dxl_timeout)

    def run(self, script: str, token: str, context: str) -> tuple[str, Reader]:
        """Runs a script and returns its checked answer. An ERROR declared by the script
        becomes a message with a name; an answer outside the protocol is almost always a
        message of the DXL interpreter, passed on with its text."""
        kind, reader = parse(self._dxl(script), token)
        if kind == "ERROR":
            code = reader.text()
            if code == "NO_MODULE":
                raise DoorsError(f"DOORS could not open the module '{context}'. Check the full path and that the "
                                 "user can read it.")
            if code == "NO_FOLDER":
                raise DoorsError(f"DOORS could not open the folder or project '{context}'.")
            raise DoorsError(f"the DXL script reported '{code}' on '{context}'.")
        return kind, reader

    # --- queries ------------------------------------------------------------------------
    def modules_of(self, folder: str) -> list[str]:
        """Full paths of every formal module under a folder or project."""
        token = self.new_token()
        _, reader = self.run(script_modules(folder, token, self.run_limit), token, folder)
        return [reader.text() for _ in range(reader.integer())]

    def attributes_of(self, module_path: str) -> list[tuple[str, str, bool]]:
        """The object attributes of the module, as (name, type, is_system)."""
        token = self.new_token()
        _, reader = self.run(script_attributes(module_path, token, self.run_limit), token, module_path)
        return [(reader.text(), reader.text(), reader.boolean()) for _ in range(reader.integer())]

    def page(self, module_path: str, attributes: Sequence[str], *, cursor: int | None, page_size: int,
             max_attribute_chars: int, links: bool = True,
             link_module: bool = True) -> tuple[list[DoorsObject], int | None]:
        """One page of objects and the next cursor (None at the end of the module)."""
        token = self.new_token()
        _, reader = self.run(script_page(module_path, attributes, token, cursor=cursor, page_size=page_size,
                                         max_attribute_chars=max_attribute_chars, links=links,
                                         link_module=link_module, run_limit=self.run_limit), token, module_path)
        return read_page(reader, module_path, attributes, links)

    def check(self, module_path: str) -> list[tuple[str, bool, str, str]]:
        """(primitive, ok, value or error, what depends on it) for every CHECKS entry."""
        results = []
        for name, body, needed_for in CHECKS:
            token = self.new_token()
            try:
                _, reader = self.run(script_check(module_path, body, token), token, module_path)
                results.append((name, True, reader.text(), needed_for))
            except DoorsError as e:
                results.append((name, False, str(e)[:300], needed_for))
                if self.blocked:
                    break
        return results


# -------------------------------------------------------------------------------------------
# Download
# -------------------------------------------------------------------------------------------

@dataclass
class Options:
    page_size: int = 100
    max_attribute_chars: int = 20_000
    attributes: tuple[str, ...] = ()
    include_system: bool = False
    links: bool = True
    link_module: bool = True


def download_module(session: DoorsSession, store: RawStore, module_path: str, options: Options) -> Stats:
    """Downloads a whole module to the raw copy and returns its statistics. The order of
    the steps matters:

    1. Resolve and check the attributes **before** writing anything: finding a misspelt
       name halfway would leave the copy half done.
    2. Walk the module by pages, every page in its own transaction.
    3. Mark the missing ones as deleted **only if the end of the module was reached**.
    4. Close the history always, on success and on failure."""
    stats = Stats(module_path=module_path)
    run_id = store.start_run(module_path)
    try:
        definitions = session.attributes_of(module_path)
        attributes = _choose_attributes(definitions, module_path, options)
        log.info("%s: %d attributes per object", module_path, len(attributes))
        with store.transaction():
            store.register_module(module_path)
            store.save_attribute_definitions(module_path, definitions)
        seen: list[int] = []
        cursor: int | None = None
        while True:
            objects, cursor = session.page(module_path, attributes, cursor=cursor, page_size=options.page_size,
                                           max_attribute_chars=options.max_attribute_chars, links=options.links,
                                           link_module=options.link_module)
            with store.transaction():
                for obj in objects:
                    seen.append(obj.absolute_number)
                    stats.count(store.save(obj, position=len(seen)))
                    stats.links += len(obj.links)
            stats.pages += 1
            log.info("%s: page %d, %d objects (%d in total)", module_path, stats.pages, len(objects), stats.seen)
            if cursor is None:
                stats.completed_module = True
                break
        # Marking the missing ones depends on having seen the end of the module, not on
        # the absence of errors: with a partial walk living objects would be deleted.
        with store.transaction():
            stats.deleted = store.mark_missing(module_path, seen)
            store.note_freshness(module_path, complete=True)
    except BaseException as e:
        stats.error = f"{type(e).__name__}: {e}"
        store.finish_run(run_id, stats)
        raise
    store.finish_run(run_id, stats)
    return stats


def _choose_attributes(definitions: list[tuple[str, str, bool]], module_path: str,
                       options: Options) -> tuple[str, ...]:
    """Which attributes are read. By default **every** object attribute of the module but
    the system ones (Created By, Last Modified On...), which are bulky and rarely useful;
    Object Heading and Object Text are system attributes but always kept, because they are
    the requirement. With --attributes, a list checked against the real module."""
    available = {name for name, _type, _system in definitions}
    if options.attributes:
        unknown = [n for n in options.attributes if n not in available]
        if unknown:
            raise DoorsError(f"the module '{module_path}' has no attributes {', '.join(repr(n) for n in unknown)}. "
                             f"Available: {', '.join(sorted(available))}")
        return tuple(options.attributes)
    essential = ("Object Heading", "Object Text")
    chosen = [n for n, _type, system in definitions if n in essential or options.include_system or not system]
    if not chosen:
        raise DoorsError(f"the module '{module_path}' declares no object attribute.")
    return tuple(chosen)


def download(module_paths: Sequence[str], db_path: str, session: DoorsSession, options: Options) -> list[Stats]:
    """Downloads several modules; the failure of one does not stop the others (a module the
    user cannot read is normal in a large project). What was saved of the previous modules
    is consistent, because every page was committed in its own transaction."""
    results = []
    with RawStore(db_path) as store:
        for n, module_path in enumerate(module_paths, start=1):
            log.info("[%d/%d] Downloading %s", n, len(module_paths), module_path)
            try:
                stats = download_module(session, store, module_path, options)
            except DoorsError as e:
                log.error("%s: %s", module_path, e)
                results.append(Stats(module_path=module_path, error=str(e)))
                if session.blocked:
                    # after a timeout the session takes no more calls: going on would only
                    # repeat the same error
                    log.error("Session blocked: the remaining modules are left.")
                    break
                continue
            log.info("%s", stats.summary())
            results.append(stats)
    return results


# -------------------------------------------------------------------------------------------
# Command line
# -------------------------------------------------------------------------------------------

def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Downloads DOORS Classic modules to a raw SQLite copy.",
                                     epilog="Defaults can be set with DOORS_MODULE_PATH, DOORS_RAW_DB and "
                                            "DOORS_PROG_ID. Then: publish.py, to the exchange database of atlas.")
    parser.add_argument("--module", action="append", default=[], metavar="PATH",
                        help="full path of a formal module; can be repeated")
    parser.add_argument("--folder", action="append", default=[], metavar="PATH",
                        help="folder or project whose modules are all downloaded, recursively")
    parser.add_argument("--db", default=os.environ.get("DOORS_RAW_DB", "doors_raw.sqlite"),
                        help="SQLite file of the raw copy (default: doors_raw.sqlite)")
    parser.add_argument("--attributes", default="", metavar="A,B,C",
                        help="attributes to download; by default, every one of the module")
    parser.add_argument("--include-system", action="store_true",
                        help="also the system attributes (Created By, Last Modified On...)")
    parser.add_argument("--no-links", action="store_true", help="do not read the outgoing links")
    parser.add_argument("--no-link-module", action="store_true",
                        help="read the links without their link module (if --check says module(l) fails)")
    parser.add_argument("--page", type=int, default=100, help="objects per DXL call")
    parser.add_argument("--max-attribute-chars", type=int, default=20_000,
                        help="limit of characters per attribute; the rest is cut with a mark")
    parser.add_argument("--prog-id", default=os.environ.get("DOORS_PROG_ID", "DOORS.Application"))
    parser.add_argument("--start-timeout", type=int, default=60, metavar="SECONDS")
    parser.add_argument("--dxl-timeout", type=int, default=300, metavar="SECONDS")
    parser.add_argument("--list-only", action="store_true", help="list the modules that would be downloaded")
    parser.add_argument("--check", action="store_true",
                        help="check the DXL primitives of the hierarchy and the links on the first module")
    parser.add_argument("-v", "--verbose", action="store_true", help="detailed trace")
    args = parser.parse_args(argv)
    if not args.module and not args.folder:
        from_env = os.environ.get("DOORS_MODULE_PATH", "").strip()
        if not from_env:
            parser.error("give at least one --module or --folder")
        args.module = [from_env]
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    options = Options(page_size=args.page, max_attribute_chars=args.max_attribute_chars,
                      attributes=tuple(n.strip() for n in args.attributes.split(",") if n.strip()),
                      include_system=args.include_system, links=not args.no_links,
                      link_module=not args.no_link_module)
    try:
        with DoorsSession(prog_id=args.prog_id, start_timeout=args.start_timeout,
                          dxl_timeout=args.dxl_timeout) as session:
            modules = list(args.module)
            for folder in args.folder:
                found = session.modules_of(folder)
                log.info("%s: %d formal modules", folder, len(found))
                modules.extend(found)
            # without duplicates and in a stable order: a folder and a loose module can name
            # the same module
            modules = list(dict.fromkeys(modules))
            if not modules:
                log.error("There is no module to download.")
                return 1
            if args.list_only:
                print("\n".join(modules))
                return 0
            if args.check:
                results = session.check(modules[0])
                print(f"DXL primitives on {modules[0]}:")
                for name, ok, value, needed_for in results:
                    print(f"  {'OK  ' if ok else 'FAIL'} {name:36s} {value[:80]!r}" + ("" if ok else f"  -> {needed_for}"))
                return 0 if all(ok for _, ok, _, _ in results) else 1
            results = download(modules, args.db, session, options)
    except DoorsError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        log.warning("Download interrupted by the user.")
        return 130
    failed = [r for r in results if r.error]
    print(f"\nRaw copy: {args.db}")
    for stats in results:
        print(f"  {stats.summary()}" if not stats.error else f"  {stats.module_path}: FAILED")
    if failed:
        print(f"\n{len(failed)} module(s) with errors:")
        for stats in failed:
            print(f"  {stats.module_path}: {stats.error}")
        return 1
    print("Next: python tools/doors_export/publish.py --raw ... --mapping mapping.toml --out requirements.sqlite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
