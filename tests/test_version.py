"""One version number, declared in five places that cannot import each other.

A Python package, a build manifest, an Inno Setup script, a PyInstaller spec
and a compose file each need it written out. Nothing links them, so they drift
silently - and the symptom is an installer named 1.3.0 that installs an
application calling itself 1.0.0.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from cloudtransfer import __license__, __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _search(path: str, pattern: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(pattern, text, re.MULTILINE)
    assert match, f"no version found in {path} with {pattern!r}"
    return match.group(1)


def test_the_package_version_is_a_release_number():
    assert SEMVER.match(__version__), __version__


@pytest.mark.parametrize(
    "path,pattern",
    [
        ("pyproject.toml", r'^version = "([^"]+)"'),
        ("packaging/installer.iss", r'^\s*#define AppVersion\s+"([^"]+)"'),
        ("packaging/cloudtransfer.spec", r'"CFBundleShortVersionString": "([^"]+)"'),
        ("docker-compose.yml", r"^\s*image: cloudtransfer:(\S+)"),
    ],
)
def test_every_file_agrees_with_the_package(path, pattern):
    assert _search(path, pattern) == __version__, (
        f"{path} is out of step with cloudtransfer.__version__"
    )


# ------------------------------------------------------------------ licence

LICENCE = ROOT / "LICENSE"


def test_the_licence_file_is_present_and_whole():
    """A truncated or missing licence silently makes the project non-free."""
    assert LICENCE.exists(), "LICENSE is missing from the repository root"
    text = LICENCE.read_text(encoding="utf-8")

    assert text.startswith("                    GNU GENERAL PUBLIC LICENSE")
    assert "Version 3, 29 June 2007" in text
    assert "Copyright (C) 2007 Free Software Foundation, Inc." in text
    assert "END OF TERMS AND CONDITIONS" in text
    assert text.rstrip().endswith("<https://www.gnu.org/licenses/why-not-lgpl.html>.")

    # All eighteen numbered sections, 0 through 17.
    numbered = {int(n) for n in re.findall(r"^\s{2}(\d{1,2})\. ", text, re.MULTILINE)}
    assert numbered == set(range(18)), f"sections present: {sorted(numbered)}"


def test_the_declared_licence_matches_the_file():
    assert __license__ == "GPL-3.0-only"
    assert _search("pyproject.toml", r'^license = "([^"]+)"') == __license__


def test_the_licence_travels_with_the_application():
    """The GPL asks that the licence accompany the program, so both the
    installer and a plain frozen build have to carry it."""
    installer = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert "LICENSE" in installer, "the installer does not install the licence"

    spec = (ROOT / "packaging" / "cloudtransfer.spec").read_text(encoding="utf-8")
    assert "LICENSE" in spec, "the frozen build does not include the licence"
