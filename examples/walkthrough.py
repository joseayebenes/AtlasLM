#!/usr/bin/env python3
"""A walk through atlas with the configuration given: each action as an engineer types it,
what it prints and how long it took. Over the samples it is the script of a course; over
a new workstation (Windows included), its smoke test.

    python examples/walkthrough.py [atlas.toml] [--id ID] [--module MODULE] [--search TEXT]
                                   [--ask TEXT] [--agent TEXT] [--skip a,b] [--lines N]
    make demo CONFIG=atlas.test.toml

The actions run in the current folder, as `atlas` itself would: the paths of the sources
are relative to it. With `model = "fake"` and nothing answering at its local base_url, the
fake API of the tests (tests/fakes/fake_api.py) is started there for the walk. If
`atlas check` fails, only the actions that need no model run. The agent runs last, and only
with the [agents] extra. The exit code is 1 if an action failed.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.config import ConfigError, load_config  # noqa: E402

# (name, arguments, what needs: "" nothing | "model" the chat and embeddings | "agents" also ADK, what to look at)
STEPS = [
    ("version", ["--version"], "", "the version installed"),
    ("check", ["check", "{config}"], "", "one request to the chat model and one to the embeddings"),
    ("sections", ["sections", "{config}"], "", "the sections of each document, without the model"),
    ("run", ["run", "{config}"], "model", "incremental processing: the first time, everything"),
    ("run again", ["run", "{config}"], "model", "nothing changed: no unit processed, no request"),
    ("status", ["status", "{config}"], "", "the data, the ledger and the time profile of each run"),
    ("graph", ["graph", "{config}"], "", "the graph of code, DOORS and documents, and its gaps"),
    ("trace", ["trace", "{id}", "{config}"], "", "a requirement in one call: DOORS, documents, code, tests"),
    ("matrix", ["matrix", "{module}", "{config}"], "", "the traceability matrix of a module, in CSV and Markdown"),
    ("search", ["search", "{search}", "{config}"], "model", "hybrid search: identifiers, words and vectors"),
    ("ask", ["ask", "{ask}", "{config}"], "model", "an answer with citations [n] to its sources"),
    ("agent", ["agent", "assistant", "{agent}", "{config}"], "agents", "the assistant and the tools it called"),
]


def ensure_samples() -> None:
    """examples/samples, generated if missing (the PDFs need pymupdf)."""
    from examples import make_samples
    samples = ROOT / "examples" / "samples"
    if not (samples / "requirements.sqlite").exists():
        make_samples.ada(); make_samples.doors(); make_samples.exchange()
    if not (samples / "docs" / "spec_without_bookmarks.pdf").exists():
        try:
            make_samples.pdfs()
        except ImportError:
            print("pymupdf not installed: the sample PDFs are not generated")


def has_adk() -> bool:
    try:
        return importlib.util.find_spec("google.adk") is not None
    except ImportError:                     # not even the google namespace
        return False


def _answers(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=0.5)
        return True
    except urllib.error.HTTPError:          # it answers, even if not on that path
        return True
    except Exception:
        return False


def start_fake(cfg) -> subprocess.Popen | None:
    """The fake API at the base_url of [llm], if that is what the configuration asks for
    and nothing answers there yet."""
    llm = cfg.get("llm", {})
    url = urllib.parse.urlparse(llm.get("base_url", ""))
    if llm.get("model") != "fake" or url.hostname not in ("127.0.0.1", "localhost") or not url.port:
        return None
    root = f"http://{url.hostname}:{url.port}/"
    if _answers(root):
        return None
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fakes" / "fake_api.py"), "--port", str(url.port)],
                            stdout=subprocess.DEVNULL)
    for _ in range(50):
        if _answers(root):
            print(f"fake API started at {root}v1 for the walk")
            return proc
        time.sleep(0.1)
    proc.terminate()
    sys.exit(f"the fake API did not start at {root}")


def run_step(argv: list[str], lines: int) -> tuple[int, float]:
    """Runs `atlas` with argv in the current folder and prints its output (the first
    `lines` lines; 0 = all). (exit code, seconds)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if (ROOT / "atlas").is_dir():               # from the repository: its atlas, not another one
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH")]))
    t0 = time.perf_counter()
    res = subprocess.run([sys.executable, "-m", "atlas.cli", *argv], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=env)
    seconds = time.perf_counter() - t0
    out = res.stdout.decode("utf-8", errors="replace").rstrip().splitlines()
    shown = out if not lines or len(out) <= lines else out[:lines]
    for line in shown:
        print(f"  {line}")
    if len(shown) < len(out):
        print(f"  … {len(out) - len(shown)} more lines (--lines 0 shows them all)")
    return res.returncode, seconds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("config", nargs="?", default="atlas.toml")
    ap.add_argument("--id", default="SRS-0004", help="requirement for trace")
    ap.add_argument("--module", default="SRS_TMS", help="DOORS module for matrix")
    ap.add_argument("--search", default="track deletion latency")
    ap.add_argument("--ask", default="¿Cuánto tarda en borrarse una traza?")
    ap.add_argument("--agent", default="¿Qué prueba verifica SRS-0004?", help="question to the assistant")
    ap.add_argument("--skip", default="", help="actions left out, separated by commas: ask,agent")
    ap.add_argument("--lines", type=int, default=40, help="lines of output shown per action (0 = all)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")     # a Windows console that cannot show a character
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        sys.exit(f"configuration: {e}")
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    unknown = skip - {name for name, *_ in STEPS}
    if unknown:
        sys.exit(f"--skip: unknown actions {sorted(unknown)} (valid: {', '.join(n for n, *_ in STEPS)})")
    ensure_samples()
    print(f"configuration: {args.config}; workspace: {cfg.workspace}"
          + (" (it exists: the first run only brings it up to date)" if (cfg.workspace / "atlas.db").exists() else ""))
    fake = start_fake(cfg)
    values = {"config": args.config, "id": args.id, "module": args.module, "search": args.search,
              "ask": args.ask, "agent": args.agent}
    results: list[tuple[str, float | None, str]] = []
    model = True
    try:
        for n, (name, template, needs, what) in enumerate(STEPS, 1):
            if name in skip:
                results.append((name, None, "skipped (--skip)"))
                continue
            if needs and not model:
                results.append((name, None, "skipped: no model (atlas check failed)"))
                continue
            if needs == "agents" and not has_adk():
                results.append((name, None, "skipped: no [agents] extra"))
                continue
            command = [v.format(**values) for v in template]
            print(f"\n== {n}. {name}: {what}")
            print("$ atlas " + " ".join(f'"{c}"' if " " in c else c for c in command), flush=True)
            code, seconds = run_step(command, args.lines)
            print(f"  ({seconds:.1f} s)", flush=True)
            results.append((name, seconds, "ok" if code == 0 else f"failed (exit code {code})"))
            if name == "check" and code != 0:
                model = False
    finally:
        if fake:
            fake.terminate()
            fake.wait(timeout=5)
    print(f"\n{'action':12s} {'seconds':>8s}  result")
    for name, seconds, result in results:
        print(f"{name:12s} {'-' if seconds is None else f'{seconds:.1f}':>8s}  {result}")
    print(f"{'total':12s} {sum(s for _, s, _ in results if s):8.1f}")
    return 1 if any(r.startswith("failed") for _, _, r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
