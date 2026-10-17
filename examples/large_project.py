#!/usr/bin/env python3
"""A large synthetic project, to measure the incremental engine (phase 2 of the plan).

    python examples/large_project.py [DEST]       generates DEST (examples/large by default)
                                                  and measures three runs with the fake API

DEST/
  docs/spec_NN.pdf        specifications with bookmarks: chapters > function groups >
                          subsections of requirements ("Req: SRS-00001 ... within 120 ms")
  ada/src/*.ads, *.adb    packages whose subprograms call those of other packages and cite
                          requirements; ada/tests/*.adb, test procedures that call them
  requirements.sqlite     exchange database: SSS_LARGE and SRS_LARGE, with headings,
                          hierarchy and "satisfies" links
  atlas.toml              every step, the PDFs analysed with the model ("llm")

By default, 20 PDFs (250 requirements each, ~14 pages), 300 packages (3000 subprograms),
60 tests, 500 SSS and 5000 SRS. The measurement runs `atlas run` three times: the first one,
a second one without changes (it must make no request to the API and take less than a
minute) and one after changing the deadline of one requirement in one PDF (the text of
one page: only its section must go to the model again). Then it times the traceability
matrix and the gaps on the graph it built (`measure_graph`):

    python examples/large_project.py --graph [DEST]     only that, on a project already run

Requires: pip install pymupdf (and the [docs,code] extras to run atlas on it)
"""
from __future__ import annotations

import contextlib
import io
import json
import random
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {"pdfs": 20, "units": 300, "per_unit": 10, "tests": 60, "sss": 500, "srs": 5000}
CHAPTERS, GROUPS, SUBSECTIONS = 5, 5, 2           # of each PDF; the requirements are shared out among them


def spec_pdf(path: Path, number: int, first: int, count: int, units: int, override: dict | None = None) -> int:
    """PDF `number` with the requirements SRS-<first> .. SRS-<first + count - 1>. Its text
    only depends on its arguments, and `override` ({"SRS-00012": 999}) changes the deadline
    of some requirements: the rest of the PDF, page by page, has the same text. Returns
    its pages."""
    import pymupdf
    rnd = random.Random(number)
    doc, toc = pymupdf.open(), []
    state = {"page": None, "y": 0, "n": 0}

    def new_page():
        state["page"] = doc.new_page(width=595, height=842)
        state["n"] += 1
        state["page"].insert_text((72, 40), "COMPANY - Company Confidential", fontsize=8)
        state["page"].insert_text((280, 815), f"Page {state['n']}", fontsize=8)
        state["y"] = 80

    def line(text, x=72, size=10, gap=14):
        if state["y"] > 780:
            new_page()
        state["page"].insert_text((x, state["y"]), text, fontsize=size)
        state["y"] += gap

    new_page()
    line(f"Track Management Software Requirements Specification, part {number}", size=14, gap=28)
    per = -(-count // (CHAPTERS * GROUPS * SUBSECTIONS))
    n = first
    for c in range(1, CHAPTERS + 1):
        line(f"{c} Chapter {c} of part {number}", size=14, gap=22)
        toc.append([1, f"{c} Chapter {c} of part {number}", state["n"]])
        for g in range(1, GROUPS + 1):
            line(f"{c}.{g} Function group {c}.{g}", size=12, gap=20)
            toc.append([2, f"{c}.{g} Function group {c}.{g}", state["n"]])
            for s in range(1, SUBSECTIONS + 1):
                line(f"{c}.{g}.{s} Track handling {c}.{g}.{s}", size=11, gap=18)
                for _ in range(per):
                    if n >= first + count:
                        break
                    rid = f"SRS-{n:05d}"
                    ms = (override or {}).get(rid, rnd.randint(10, 500))
                    unit = rnd.randrange(units)
                    line(f"Req: {rid} The TMF shall process track event {n} within {ms} ms after")
                    line(f"reception (see SSS-{n % 500 + 1:05d}); see Pkg_{unit:03d}.Op_{n % 10}.", gap=16)
                    n += 1
    doc.set_toc(toc)
    doc.save(path)
    return state["n"]


def ada(dest: Path, units: int, per_unit: int, tests: int, srs: int) -> int:
    """Packages Pkg_000..: Op_0.. call two operations of other packages and, six times out of
    ten, cite a requirement. Returns the number of files."""
    rnd = random.Random(7)
    src, tst = dest / "src", dest / "tests"
    src.mkdir(parents=True, exist_ok=True)
    tst.mkdir(parents=True, exist_ok=True)
    for u in range(units):
        name = f"Pkg_{u:03d}"
        calls = {k: [(rnd.randrange(units), rnd.randrange(per_unit)) for _ in range(2)] for k in range(per_unit)}
        spec = [f"package {name} is"]
        for k in range(per_unit):
            spec.append(f"   -- Operation {k} of package {u}.")
            if rnd.random() < 0.6:
                spec.append(f"   -- Req: SRS-{rnd.randrange(1, srs + 1):05d}")
            spec.append(f"   procedure Op_{k} (X : Integer);")
        spec.append(f"end {name};")
        (src / f"pkg_{u:03d}.ads").write_text("\n".join(spec) + "\n")
        withs = sorted({f"Pkg_{v:03d}" for k in calls for v, _ in calls[k] if v != u})
        body = [f"with {w};" for w in withs] + [f"package body {name} is"]
        for k in range(per_unit):
            body += [f"   procedure Op_{k} (X : Integer) is", "   begin"]
            body += [f"      {'' if v == u else f'Pkg_{v:03d}.'}Op_{j} (X);" for v, j in calls[k]]
            body += [f"   end Op_{k};"]
        body.append(f"end {name};")
        (src / f"pkg_{u:03d}.adb").write_text("\n".join(body) + "\n")
    for t in range(tests):
        u, k = rnd.randrange(units), rnd.randrange(per_unit)
        (tst / f"test_{t:03d}.adb").write_text(
            f"with Pkg_{u:03d};\nprocedure Test_{t:03d} is\n   -- Req: SRS-{rnd.randrange(1, srs + 1):05d}\n"
            f"begin\n   Pkg_{u:03d}.Op_{k} (1);\nend Test_{t:03d};\n")
    return 2 * units + tests


def requirements(path: Path, sss: int, srs: int) -> None:
    """SSS_LARGE and SRS_LARGE in the exchange database, a heading every 50 requirements."""
    sys.path.insert(0, str(ROOT))
    from atlas.doors.exchange import ExchangeWriter
    path.unlink(missing_ok=True)
    rnd = random.Random(3)
    with ExchangeWriter(path, filler="examples/large_project.py") as w:
        for module, prefix, count in (("SSS_LARGE", "SSS", sss), ("SRS_LARGE", "SRS", srs)):
            reqs, links = [], []
            for n in range(1, count + 1):
                if n % 50 == 1:
                    head = f"{prefix}-H{n // 50:04d}"
                    reqs.append({"id": head, "kind": "heading", "heading": f"Area {n // 50 + 1}", "level": 1,
                                 "outline": str(n // 50 + 1), "source_key": head})
                rid = f"{prefix}-{n:05d}"
                text = (f"The mission system shall provide capability {n}." if prefix == "SSS" else
                        f"The TMF shall process track event {n} within {rnd.randint(10, 500)} ms.")
                reqs.append({"id": rid, "text": text, "parent": head, "level": 2,
                             "outline": f"{n // 50 + 1}.{(n - 1) % 50 + 1}", "source_key": rid,
                             "attributes": {"status": rnd.choice(["Approved", "Draft"]),
                                            "verification": rnd.choice(["Test", "Analysis", "Inspection"])}})
                if prefix == "SRS":
                    links.append({"source": rid, "target": f"SSS-{n % sss + 1:05d}", "type": "satisfies",
                                  "target_module": "SSS_LARGE"})
            w.replace_module(module, reqs, links, source=f"/Large/{module}")


def config(dest: Path, base_url: str) -> Path:
    path = dest / "atlas.toml"
    path.write_text(f'''# The large synthetic project of examples/large_project.py, with tests/fakes/fake_api.py
[project]
name = "large"
workspace = "{(dest / 'workspace').as_posix()}"
log_level = "WARNING"

[llm]
base_url = "{base_url}"
model = "fake"
api_key = "local"

[embeddings]
model = "embeddinggemma"
batch = 64

[network]
proxy = ""
no_proxy = "*"

[pipeline]
workers = 4
steps = ["doors", "code", "docs", "graph", "index"]

[[sources.docs]]
name = "specs"
paths = ["{(dest / 'docs').as_posix()}"]
patterns = ["*.pdf"]
analysis = "llm"
structure = "bookmarks"

[sources.doors]
adapter = "exchange"

[sources.doors.exchange]
path = "{(dest / 'requirements.sqlite').as_posix()}"

[sources.doors.convention]
id_pattern = "^(?:SRS|SSS)-(?:\\\\d{{5}}|H\\\\d{{4}})$"

[sources.code]
roots = ["{(dest / 'ada').as_posix()}"]
test_patterns = ["**/tests/**"]
''', encoding="utf-8")
    return path


def per_pdf(srs: int, pdfs: int) -> int:
    return -(-srs // pdfs)


def generate(dest: Path, base_url: str = "http://127.0.0.1:8770/v1", **sizes) -> dict:
    """Writes the project in `dest` (see the module docstring); `sizes` overrides DEFAULTS."""
    s = {**DEFAULTS, **sizes}
    dest = Path(dest)
    (dest / "docs").mkdir(parents=True, exist_ok=True)
    pages = sum(spec_pdf(dest / "docs" / f"spec_{k:02d}.pdf", k, 1 + k * per_pdf(s["srs"], s["pdfs"]),
                         per_pdf(s["srs"], s["pdfs"]), s["units"]) for k in range(s["pdfs"]))
    files = ada(dest / "ada", s["units"], s["per_unit"], s["tests"], s["srs"])
    requirements(dest / "requirements.sqlite", s["sss"], s["srs"])
    config(dest, base_url)
    return {**s, "pages": pages, "ada_files": files}


def measure_graph(cfg) -> dict[str, float]:
    """CPU seconds (the least of five runs: wall time depends on what else the machine is
    doing) of matrix and gaps of SRS_LARGE on the graph of the workspace, each on a new
    KnowledgeGraph (cold, as the first question of a server), and of another page of the
    matrix after gaps (warm)."""
    from atlas.graph import KnowledgeGraph
    base = KnowledgeGraph.from_config(cfg)
    base.covering_tests([])                   # computed once per graph: outside the measure
    out = {}

    def fresh():
        g = KnowledgeGraph(base.G, base.root)
        g._covering = base._covering
        return g

    def clock(label, query, warm=None):
        best = float("inf")
        for _ in range(5):
            g = fresh()
            if warm:
                warm(g)
            t = time.process_time()
            query(g)
            best = min(best, time.process_time() - t)
        out[label] = best

    clock("matrix(SRS_LARGE, limit=50)", lambda g: g.matrix("SRS_LARGE", limit=50))
    clock("matrix(SRS_LARGE, limit=50, offset=2500)", lambda g: g.matrix("SRS_LARGE", limit=50, offset=2500))
    clock("gaps(SRS_LARGE)", lambda g: g.gaps("SRS_LARGE"))
    clock("gaps()", lambda g: g.gaps())
    clock("matrix(SRS_LARGE, limit=50, offset=50) after gaps()",
          lambda g: g.matrix("SRS_LARGE", limit=50, offset=50), warm=lambda g: g.gaps())
    print("\n== graph queries (CPU): " + ", ".join(f"{k} {v:.3f} s" for k, v in out.items()), flush=True)
    return out


def _measure(dest: Path) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    control = dest / "faults.json"
    dest.mkdir(parents=True, exist_ok=True)
    control.write_text("{}")
    fake = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fakes" / "fake_api.py"), "--port", str(port),
                             "--control", str(control)])
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2)
                break
            except OSError:
                time.sleep(0.1)
        stats = lambda: json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/"))
        t0 = time.time()
        sizes = generate(dest, f"http://127.0.0.1:{port}/v1")
        print(f"generated in {time.time() - t0:.0f} s: {sizes}")
        sys.path.insert(0, str(ROOT))
        from atlas.cli import run
        from atlas.config import load_config
        cfg = load_config(dest / "atlas.toml")

        def measure(label: str) -> None:
            before, t, out = stats(), time.time(), io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = run(cfg)
            after, report = stats(), json.loads(out.getvalue())
            print(f"\n== {label}: {time.time() - t:.1f} s, exit {rc}, requests: "
                  f"{after['chat'] - before['chat']} chat, {after['embeddings'] - before['embeddings']} embeddings "
                  f"({after['emb_texts'] - before['emb_texts']} texts)\n   processed {report['processed']}"
                  f"\n   up to date {report['skipped']}", flush=True)

        measure("first run")
        measure("second run, without changes")
        first = 1 + per_pdf(sizes["srs"], sizes["pdfs"])
        spec_pdf(dest / "docs" / "spec_01.pdf", 1, first, per_pdf(sizes["srs"], sizes["pdfs"]), sizes["units"],
                 override={f"SRS-{first + 7:05d}": 999})
        measure("one deadline changed in one page of spec_01.pdf")
        measure_graph(cfg)
    finally:
        fake.terminate()
        fake.wait(timeout=5)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--graph"]
    dest = Path(args[0]) if args else ROOT / "examples" / "large"
    if "--graph" in sys.argv:
        sys.path.insert(0, str(ROOT))
        from atlas.config import load_config
        measure_graph(load_config(dest / "atlas.toml"))
    else:
        _measure(dest)
