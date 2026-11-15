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

from cloudtransfer import __version__

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
