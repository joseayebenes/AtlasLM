#!/usr/bin/env python3
"""Third-party notices and a minimal SBOM of what atlaslm installs.

    python tools/licenses.py [--out DIR] [--extras all] [--wheelhouse DIR]

By default, from the installed environment (make install: `pip install -e ".[all,dev]" -c
constraints.txt`): the dependencies of atlaslm[all] in pyproject.toml, followed through the
metadata of the installed packages (importlib.metadata) with the markers of this Python and
this system. With --wheelhouse, from the wheels (and sdists) of a wheelhouse instead: all
of them, also those of another system that are not installed here (make wheelhouse-windows).

Writes DIR/THIRD_PARTY_NOTICES.md (name, version, license and URL of each package, grouped
by license, AGPL/GPL first) and DIR/sbom.cdx.json (CycloneDX 1.5: name, version, purl and
license of each component). The license is the one the package declares: License-Expression,
else License when it is an SPDX identifier or a known alias, else its classifiers, else the
License text as it is. Warnings on stderr: an installed version other than the one in
constraints.txt. Exit status 1 if a dependency is not installed (the notices would be
incomplete).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import uuid
import zipfile
from datetime import datetime, timezone
from email.parser import HeaderParser
from importlib import metadata
from pathlib import Path

from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement

try:
    import tomllib
except ImportError:                         # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
NOTICES, SBOM = "THIRD_PARTY_NOTICES.md", "sbom.cdx.json"
SELF = "atlaslm"
OURS = {SELF, "hephestolm"}              # the packages of this repository: not third parties

# License texts and classifiers that mean one SPDX identifier (lowercase, single spaces).
ALIASES = {
    "mit": "MIT", "mit license": "MIT", "the mit license (mit)": "MIT",
    "apache 2.0": "Apache-2.0", "apache-2.0": "Apache-2.0", "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0", "apache license version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "bsd-2-clause": "BSD-2-Clause", "bsd-3-clause": "BSD-3-Clause", "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "mpl-2.0": "MPL-2.0", "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "psf-2.0": "PSF-2.0", "psfl": "PSF-2.0", "python software foundation license": "PSF-2.0",
    "isc": "ISC", "isc license (iscl)": "ISC",
}
SPDX_IDS = set(ALIASES.values()) | {"0BSD", "Zlib", "CC0-1.0", "Unlicense", "BSL-1.0", "Python-2.0",
                                    "LGPL-2.1-or-later", "LGPL-3.0-or-later", "GPL-2.0-or-later",
                                    "GPL-3.0-or-later", "AGPL-3.0-only", "AGPL-3.0-or-later"}
STRONG_COPYLEFT = re.compile(r"\bA?GPL|affero|general public license", re.I)
WEAK = re.compile(r"\bLGPL|lesser", re.I)
URL_LABELS = ("homepage", "home", "source", "source code", "repository", "github")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _spdx(text: str) -> str | None:
    """`text` as an SPDX expression, if it is an alias or made of known identifiers."""
    key = " ".join(text.split()).lower()
    if key in ALIASES:
        return ALIASES[key]
    tokens = [t for t in re.split(r"[\s()]+", text) if t]
    if tokens and all(t in SPDX_IDS or t in ("AND", "OR", "WITH") for t in tokens):
        return " ".join(text.split())
    return None


def license_of(md) -> tuple[str, str | None]:
    """(license as shown, SPDX expression or None) of some package metadata."""
    expr = (md.get("License-Expression") or "").strip()
    if expr:
        return expr, expr
    text = (md.get("License") or "").strip()
    first = text.splitlines()[0].strip() if text else ""
    if first and first.upper() != "UNKNOWN" and (spdx := _spdx(first)):
        return spdx, spdx
    classifiers = [c.split(" :: ")[-1] for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        names = [ALIASES.get(c.lower(), c) for c in classifiers]
        return "; ".join(names), names[0] if len(names) == 1 and names[0] in SPDX_IDS else None
    if text and "\n" not in text and text.upper() != "UNKNOWN":
        return text, None
    return "UNKNOWN", None


def url_of(md) -> str:
    if md.get("Home-page"):
        return md["Home-page"].strip()
    urls = [u.split(",", 1) for u in md.get_all("Project-URL") or [] if "," in u]
    for label, url in urls:
        if label.strip().lower() in URL_LABELS:
            return url.strip()
    return urls[0][1].strip() if urls else ""


def is_strong_copyleft(license: str) -> bool:
    return bool(STRONG_COPYLEFT.search(license)) and not WEAK.search(license)


# --- where the packages come from ------------------------------------------------------------

def root_requirements(extras: list[str]) -> list[Requirement]:
    """The dependencies of atlaslm[extras] in pyproject.toml, its own extras expanded."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    optional = project.get("optional-dependencies", {})
    reqs, pending, seen = [Requirement(r) for r in project["dependencies"]], list(extras), set()
    while pending:
        extra = pending.pop()
        if extra in seen:
            continue
        seen.add(extra)
        for line in optional[extra]:
            req = Requirement(line)
            if normalize(req.name) == SELF:
                pending.extend(req.extras)
            else:
                reqs.append(req)
    return reqs


def installed(extras: list[str]) -> tuple[list, list[str]]:
    """([metadata], [missing requirement]) of the dependencies of atlaslm[extras] installed here."""
    env = default_environment()

    def applies(req: Requirement, wanted: set[str]) -> bool:
        return req.marker is None or any(req.marker.evaluate({**env, "extra": e}) for e in wanted | {""})

    found: dict[str, tuple] = {}            # normalized name -> (metadata, extras already followed)
    missing = []
    queue = [(req, "atlaslm") for req in root_requirements(extras) if applies(req, set())]
    while queue:
        req, by = queue.pop()
        key = normalize(req.name)
        md, done = found.get(key, (None, None))
        if md is None:
            try:
                md = metadata.distribution(req.name).metadata
            except metadata.PackageNotFoundError:
                missing.append(f"{req} (required by {by})")
                continue
            done = None
        wanted = set(req.extras)
        if done is not None and wanted <= done:
            continue
        done = (done or set()) | wanted
        found[key] = (md, done)
        for line in md.get_all("Requires-Dist") or []:
            sub = Requirement(line)
            if applies(sub, done):
                queue.append((sub, md["Name"]))
    return [md for md, _ in found.values()], sorted(set(missing))


def in_wheelhouse(folder: Path) -> list:
    """Metadata of every wheel and sdist in `folder`, except atlaslm."""
    parser, result = HeaderParser(), []
    for path in sorted(folder.iterdir()):
        text = None
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as z:
                name = next(n for n in z.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n))
                text = z.read(name).decode("utf-8")
        elif path.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(path) as t:
                member = next(m for m in t.getmembers() if re.fullmatch(r"[^/]+/PKG-INFO", m.name))
                text = t.extractfile(member).read().decode("utf-8")
        elif path.suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                name = next(n for n in z.namelist() if re.fullmatch(r"[^/]+/PKG-INFO", n))
                text = z.read(name).decode("utf-8")
        if text is not None:
            md = parser.parsestr(text)
            if normalize(md["Name"]) not in OURS:
                result.append(md)
    return result


def pins(constraints: Path) -> dict[str, str]:
    """{normalized name: version} of constraints.txt for this Python and this system."""
    result = {}
    for line in constraints.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" not in line:
            continue
        spec, _, marker = line.partition(";")
        if marker.strip() and not Marker(marker.strip()).evaluate():
            continue
        name, _, version = spec.partition("==")
        result[normalize(name.strip())] = version.strip()
    return result


# --- output ------------------------------------------------------------------------------------

def packages(mds: list) -> list[dict]:
    result = []
    for md in mds:
        shown, spdx = license_of(md)
        result.append({"name": md["Name"], "version": md["Version"], "license": shown, "spdx": spdx,
                       "url": url_of(md), "purl": f"pkg:pypi/{normalize(md['Name'])}@{md['Version']}"})
    return sorted(result, key=lambda p: normalize(p["name"]))


def notices(pkgs: list[dict], version: str, source: str) -> str:
    def table(rows, with_license):
        head = "| Paquete | Versión | Licencia | URL |\n|---|---|---|---|" if with_license else \
               "| Paquete | Versión | URL |\n|---|---|---|"
        lines = [f"| {p['name']} | {p['version']} | " + (f"{p['license']} | " if with_license else "")
                 + f"{p['url']} |" for p in rows]
        return "\n".join([head, *lines])

    copyleft = [p for p in pkgs if is_strong_copyleft(p["license"])]
    out = [f"# Avisos de terceros de atlaslm {version}", "",
           f"atlaslm instala estos {len(pkgs)} paquetes de terceros. Cada uno tiene su propia licencia, "
           "la que declaran sus metadatos; el texto completo va dentro de cada wheel "
           "(`*.dist-info/licenses/` o `*.dist-info/LICENSE*`).", "",
           f"Generado con `tools/licenses.py` el {datetime.now().date().isoformat()} {source}.", ""]
    if copyleft:
        out += ["## Copyleft fuerte: AGPL y GPL", "",
                "Estas licencias obligan a dar el código fuente a quien reciba el programa (la AGPL, "
                "también a quien lo use a través de una red). Antes de distribuir atlas fuera de la "
                "empresa, lee `docs/seguridad.md` («Dependencias»).", "",
                table(copyleft, True), ""]
    out += ["## Por licencia", ""]
    groups: dict[str, list] = {}
    for p in pkgs:
        groups.setdefault(p["license"], []).append(p)
    for license in sorted(groups, key=lambda k: (k == "UNKNOWN", k.lower())):
        out += [f"### {license} ({len(groups[license])})", "", table(groups[license], False), ""]
    return "\n".join(out)


def sbom(pkgs: list[dict], version: str) -> dict:
    def licenses(p):
        if p["spdx"] and re.search(r" (AND|OR|WITH) ", p["spdx"]):
            return [{"expression": p["spdx"]}]
        if p["spdx"]:
            return [{"license": {"id": p["spdx"]}}]
        return [{"license": {"name": p["license"]}}]

    def component(p):
        c = {"type": "library", "bom-ref": p["purl"], "name": p["name"], "version": p["version"],
             "purl": p["purl"], "licenses": licenses(p)}
        if p["url"]:
            c["externalReferences"] = [{"type": "website", "url": p["url"]}]
        return c

    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{uuid.uuid4()}",
            "version": 1,
            "metadata": {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "tools": {"components": [{"type": "application", "name": "tools/licenses.py"}]},
                         "component": {"type": "application", "bom-ref": f"pkg:pypi/{SELF}@{version}",
                                       "name": SELF, "version": version, "purl": f"pkg:pypi/{SELF}@{version}"}},
            "components": [component(p) for p in pkgs]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "dist", help="folder for the two files (dist)")
    ap.add_argument("--extras", default="all", help="extras of atlaslm to follow, comma-separated (all)")
    ap.add_argument("--wheelhouse", type=Path, help="read the packages of this wheelhouse instead")
    ap.add_argument("--constraints", type=Path, default=ROOT / "constraints.txt")
    args = ap.parse_args(argv)

    version = re.search(r'__version__ = "(.+)"', (ROOT / "atlas" / "__init__.py").read_text())[1]
    missing = []
    if args.wheelhouse:
        pkgs = packages(in_wheelhouse(args.wheelhouse))
        source = f"a partir de los paquetes de `{args.wheelhouse.name}`"
    else:
        mds, missing = installed([e.strip() for e in args.extras.split(",") if e.strip()])
        pkgs = packages(mds)
        source = (f"a partir de las dependencias de `atlaslm[{args.extras}]` instaladas con Python "
                  f"{sys.version_info.major}.{sys.version_info.minor} en {sys.platform}")
        pinned = pins(args.constraints) if args.constraints.exists() else {}
        for p in pkgs:
            pin = pinned.get(normalize(p["name"]))
            if pinned and pin != p["version"]:
                print(f"warning: {p['name']} {p['version']} is installed, but constraints.txt "
                      f"{'pins ' + pin if pin else 'does not pin it'}", file=sys.stderr)
    for m in missing:
        print(f"error: not installed: {m}", file=sys.stderr)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / NOTICES).write_text(notices(pkgs, version, source), encoding="utf-8")
    (args.out / SBOM).write_text(json.dumps(sbom(pkgs, version), indent=2) + "\n", encoding="utf-8")
    copyleft = [f"{p['name']} ({p['license']})" for p in pkgs if is_strong_copyleft(p["license"])]
    print(f"{args.out / NOTICES} and {args.out / SBOM}: {len(pkgs)} packages"
          + (f"; AGPL/GPL: {', '.join(copyleft)}" if copyleft else ""))
    if missing:
        print("some dependencies are not installed: make install", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
