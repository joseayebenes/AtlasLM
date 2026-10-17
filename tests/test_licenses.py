"""tools/licenses.py: third-party notices and SBOM, from the installed environment and from a
wheelhouse; and the license of each extractor in `atlas plugins`."""
import json
import subprocess
import sys
import tarfile
import zipfile
from email.message import Message
from io import BytesIO
from pathlib import Path

import pytest

from atlas.config import load_config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import licenses  # noqa: E402


def _md(**fields) -> Message:
    md = Message()
    for key, value in fields.items():
        for v in value if isinstance(value, list) else [value]:
            md[key.replace("_", "-")] = v
    return md


def test_license_of_each_kind_of_metadata():
    assert licenses.license_of(_md(License_Expression="MIT OR Apache-2.0", License="x")) == \
        ("MIT OR Apache-2.0", "MIT OR Apache-2.0")
    assert licenses.license_of(_md(License="Apache 2.0")) == ("Apache-2.0", "Apache-2.0")
    assert licenses.license_of(_md(License="MPL-2.0 AND MIT")) == ("MPL-2.0 AND MIT", "MPL-2.0 AND MIT")
    # the whole text in License: its first line, or else the classifiers
    assert licenses.license_of(_md(License="MIT License\n\nCopyright (c) ...")) == ("MIT", "MIT")
    assert licenses.license_of(_md(License="Some License\nfull text", Classifier=[
        "License :: OSI Approved :: BSD License", "Programming Language :: Python"])) == ("BSD License", None)
    assert licenses.license_of(_md(Classifier="License :: OSI Approved :: MIT License")) == ("MIT", "MIT")
    agpl = "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License"
    assert licenses.license_of(_md(License=agpl)) == (agpl, None)
    assert licenses.license_of(_md()) == ("UNKNOWN", None)


def test_strong_copyleft():
    assert licenses.is_strong_copyleft("AGPL-3.0-only")
    assert licenses.is_strong_copyleft("Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License")
    assert licenses.is_strong_copyleft("GNU General Public License (GPL)")
    assert not licenses.is_strong_copyleft("LGPL-3.0-or-later")
    assert not licenses.is_strong_copyleft("GNU Lesser General Public License v3 (LGPLv3)")
    assert not licenses.is_strong_copyleft("MIT")


@pytest.mark.skipif(not __import__("importlib.util").util.find_spec("pymupdf"), reason="needs pymupdf")
def test_installed_environment_lists_pymupdf(tmp_path):
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "licenses.py"), "--out", str(tmp_path)],
                         capture_output=True, text=True)
    assert "AGPL/GPL: " in res.stdout and "pymupdf" in res.stdout, res.stdout + res.stderr
    notices = (tmp_path / "THIRD_PARTY_NOTICES.md").read_text()
    copyleft = notices.split("## Copyleft fuerte")[1].split("## Por licencia")[0]
    assert "| pymupdf |" in copyleft and "AFFERO" in copyleft
    assert "| openai |" in notices and "| atlaslm |" not in notices
    bom = json.loads((tmp_path / "sbom.cdx.json").read_text())
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.5"
    assert bom["metadata"]["component"]["name"] == "atlaslm"
    pymupdf = next(c for c in bom["components"] if c["name"].lower() == "pymupdf")
    assert pymupdf["purl"] == f"pkg:pypi/pymupdf@{pymupdf['version']}"
    assert "AFFERO" in pymupdf["licenses"][0]["license"]["name"]
    assert all(c["purl"].startswith("pkg:pypi/") and c["licenses"] for c in bom["components"])


def test_wheelhouse(tmp_path):
    """From the wheels and sdists of a folder, also those not installed here; atlaslm is left out."""
    def wheel(name, version, meta):
        with zipfile.ZipFile(tmp_path / f"{name}-{version}-py3-none-any.whl", "w") as z:
            z.writestr(f"{name}/__init__.py", "")
            z.writestr(f"{name}-{version}.dist-info/METADATA",
                       f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{meta}")

    wheel("atlaslm", "9.9", "")
    wheel("winonly", "1.0", "License-Expression: BSD-3-Clause\nProject-URL: Source, https://example.org/w\n")
    wheel("strong", "2.0", "Classifier: License :: OSI Approved :: GNU General Public License v3 (GPLv3)\n")
    data = b"Metadata-Version: 2.1\nName: oldsdist\nVersion: 0.3\nLicense: MIT\nHome-page: https://example.org/o\n"
    with tarfile.open(tmp_path / "oldsdist-0.3.tar.gz", "w:gz") as t:
        info = tarfile.TarInfo("oldsdist-0.3/PKG-INFO"); info.size = len(data)
        t.addfile(info, BytesIO(data))

    out = tmp_path / "out"
    assert licenses.main(["--wheelhouse", str(tmp_path), "--out", str(out)]) == 0
    notices = (out / "THIRD_PARTY_NOTICES.md").read_text()
    assert "estos 3 paquetes" in notices and "atlaslm |" not in notices
    assert "| strong | 2.0 | GNU General Public License v3 (GPLv3) |" in notices.split("## Por licencia")[0]
    assert "| winonly | 1.0 | https://example.org/w |" in notices
    assert "### MIT (1)" in notices and "| oldsdist | 0.3 | https://example.org/o |" in notices
    bom = json.loads((out / "sbom.cdx.json").read_text())
    assert {c["purl"]: c["licenses"] for c in bom["components"]} == {
        "pkg:pypi/oldsdist@0.3": [{"license": {"id": "MIT"}}],
        "pkg:pypi/strong@2.0": [{"license": {"name": "GNU General Public License v3 (GPLv3)"}}],
        "pkg:pypi/winonly@1.0": [{"license": {"id": "BSD-3-Clause"}}]}


def test_plugins_shows_extractor_licenses(toml, capsys):
    from atlas.cli import list_plugins
    assert list_plugins(load_config(toml('[plugins]\ndisabled = ["extractor:pymupdf"]\n'))) == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("  pymupdf "))
    assert ".pdf  license: AGPL-3.0" in line and line.endswith("(disabled)")
    assert "markitdown" in out and "license: MIT (markitdown, pdfminer.six)" in out
    assert "license: no third-party library" in out
