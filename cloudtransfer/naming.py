"""Translating Google Drive names into names OneDrive will actually accept.

Drive and OneDrive disagree in ways that silently lose files if ignored:

* Drive permits ``" * : < > ? / \\ |`` in names; OneDrive rejects all of them.
* Drive permits two files with the identical name in one folder; OneDrive
  treats a name as unique within its parent.
* OneDrive inherits a set of reserved Windows names and prefixes.
* OneDrive caps a single name at 255 characters and a whole path at 400.

Every rename this module performs is recorded on the file task so the final
report can show exactly what was changed and why.
"""

from __future__ import annotations

import posixpath
import re

# Characters Microsoft Graph rejects outright in a driveItem name.
INVALID_CHARS = '"*:<>?/\\|'
_INVALID_RE = re.compile(f"[{re.escape(INVALID_CHARS)}]")

# Control characters are not documented as invalid but break path handling.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

MAX_NAME_LENGTH = 255
MAX_PATH_LENGTH = 400

# Reserved whole names, inherited from Windows device names.
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", ".lock", "desktop.ini",
    *(f"com{i}" for i in range(10)),
    *(f"lpt{i}" for i in range(10)),
}

# Reserved prefixes and infixes.
_RESERVED_PREFIX = "~$"
_RESERVED_INFIX = "_vti_"

REPLACEMENT = "_"


def _split_ext(name: str) -> tuple[str, str]:
    """Split into (stem, suffix) keeping the dot, for sensible truncation."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > 20:
        return name, ""
    return stem, dot + ext


def sanitize_name(name: str, *, fallback: str = "untitled") -> tuple[str, list[str]]:
    """Return a OneDrive-safe name plus the list of reasons it was altered."""
    reasons: list[str] = []
    original = name

    cleaned = _CONTROL_RE.sub("", name)
    if cleaned != name:
        reasons.append("removed control characters")
    name = cleaned

    cleaned = _INVALID_RE.sub(REPLACEMENT, name)
    if cleaned != name:
        reasons.append(f"replaced characters invalid on OneDrive ({INVALID_CHARS})")
    name = cleaned

    # OneDrive disallows leading/trailing whitespace and trailing dots.
    cleaned = name.strip().rstrip(".").rstrip()
    if cleaned != name:
        reasons.append("trimmed leading/trailing spaces and dots")
    name = cleaned

    if _RESERVED_INFIX in name.lower():
        name = re.sub(_RESERVED_INFIX, "_vti-", name, flags=re.IGNORECASE)
        reasons.append("rewrote reserved sequence '_vti_'")

    if name.startswith(_RESERVED_PREFIX):
        name = REPLACEMENT + name[len(_RESERVED_PREFIX) :]
        reasons.append("rewrote reserved prefix '~$'")

    if name.lower() in _RESERVED_NAMES:
        name = f"{name}{REPLACEMENT}"
        reasons.append("appended '_' to a reserved device name")

    if not name:
        name = fallback
        reasons.append("name was empty after cleaning")

    if len(name) > MAX_NAME_LENGTH:
        stem, ext = _split_ext(name)
        keep = MAX_NAME_LENGTH - len(ext)
        name = stem[: max(keep, 1)] + ext
        reasons.append(f"truncated to {MAX_NAME_LENGTH} characters")

    if name != original and not reasons:  # pragma: no cover - defensive
        reasons.append("adjusted for OneDrive")

    return name, reasons


def dedupe_name(name: str, taken: set[str]) -> tuple[str, bool]:
    """Make ``name`` unique against a case-insensitive set of sibling names.

    Mirrors how OneDrive itself renames on conflict: ``report (1).pdf``.
    The returned flag says whether a change was needed. ``taken`` is updated.
    """
    key = name.casefold()
    if key not in taken:
        taken.add(key)
        return name, False

    stem, ext = _split_ext(name)
    counter = 1
    while True:
        candidate = f"{stem} ({counter}){ext}"
        if len(candidate) > MAX_NAME_LENGTH:
            overflow = len(candidate) - MAX_NAME_LENGTH
            stem = stem[: max(len(stem) - overflow, 1)]
            candidate = f"{stem} ({counter}){ext}"
        ckey = candidate.casefold()
        if ckey not in taken:
            taken.add(ckey)
            return candidate, True
        counter += 1


def check_path_length(path: str) -> str | None:
    """Return a warning if the destination path exceeds OneDrive's limit."""
    if len(path) > MAX_PATH_LENGTH:
        return (
            f"destination path is {len(path)} characters, over OneDrive's "
            f"{MAX_PATH_LENGTH}-character limit"
        )
    return None


def join_path(*parts: str) -> str:
    """Join destination path segments, skipping empty ones.

    Returns ``""`` when nothing is left, since a root-level file has no folder
    prefix and ``posixpath.join`` needs at least one argument.
    """
    cleaned = [p.strip("/") for p in parts if p and p.strip("/")]
    if not cleaned:
        return ""
    return posixpath.join(*cleaned)
