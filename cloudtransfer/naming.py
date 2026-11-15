"""Translating a file's name from what one provider allows into what the other does.

The two sides disagree, and not symmetrically:

* **OneDrive** rejects ``" * : < > ? / \\ |``, inherits a set of reserved
  Windows device names, treats names as unique within a folder, and caps a name
  at 255 characters and a whole path at 400.
* **Google Drive** allows very nearly anything, including two siblings with the
  same name, and is case-sensitive.

So going one way needs real rewriting and going the other way needs almost
none. A :class:`NamingPolicy` describes the destination's rules, and every
rename it causes is recorded on the file task so the job report can show
exactly what changed and why.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

# Control characters break path handling everywhere, whatever the provider.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

REPLACEMENT = "_"

# Windows device names, which OneDrive inherits.
_WINDOWS_RESERVED = frozenset(
    {
        "con", "prn", "aux", "nul", ".lock", "desktop.ini",
        *(f"com{i}" for i in range(10)),
        *(f"lpt{i}" for i in range(10)),
    }
)


@dataclass(frozen=True, slots=True)
class NamingPolicy:
    """What one destination will accept as a name."""

    provider: str
    invalid_chars: str = ""
    reserved_names: frozenset[str] = frozenset()
    reserved_prefix: str | None = None
    reserved_infix: str | None = None
    max_name_length: int = 255
    max_path_length: int | None = None
    # OneDrive treats one name per folder as unique, case-insensitively.
    # Google Drive happily keeps two files called the same thing.
    unique_names: bool = True
    case_insensitive: bool = True
    # Largest single file the destination accepts.
    max_file_size: int = 250 * 1024**3
    max_file_size_label: str = "250 GB"
    _invalid_re: re.Pattern | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        pattern = (
            re.compile(f"[{re.escape(self.invalid_chars)}]")
            if self.invalid_chars
            else None
        )
        object.__setattr__(self, "_invalid_re", pattern)

    def fold(self, name: str) -> str:
        return name.casefold() if self.case_insensitive else name


ONEDRIVE = NamingPolicy(
    provider="OneDrive",
    invalid_chars='"*:<>?/\\|',
    reserved_names=_WINDOWS_RESERVED,
    reserved_prefix="~$",
    reserved_infix="_vti_",
    max_name_length=255,
    max_path_length=400,
    unique_names=True,
    case_insensitive=True,
    max_file_size=250 * 1024**3,
    max_file_size_label="250 GB",
)

GOOGLE_DRIVE = NamingPolicy(
    provider="Google Drive",
    # Drive imposes no forbidden characters of its own; even a slash is kept,
    # it simply looks odd in the interface.
    invalid_chars="",
    reserved_names=frozenset(),
    reserved_prefix=None,
    reserved_infix=None,
    # Drive's documented ceiling is far larger, but a name this long is already
    # unusable, and keeping it bounded avoids surprises elsewhere.
    max_name_length=1024,
    max_path_length=None,
    unique_names=False,
    case_insensitive=False,
    max_file_size=5 * 1024**4,
    max_file_size_label="5 TB",
)

POLICIES = {"onedrive": ONEDRIVE, "gdrive": GOOGLE_DRIVE}

# Kept so existing imports and tests keep meaning what they did.
INVALID_CHARS = ONEDRIVE.invalid_chars
MAX_NAME_LENGTH = ONEDRIVE.max_name_length
MAX_PATH_LENGTH = ONEDRIVE.max_path_length


def _split_ext(name: str) -> tuple[str, str]:
    """Split into (stem, suffix) keeping the dot, for sensible truncation."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > 20:
        return name, ""
    return stem, dot + ext


def sanitize_name(
    name: str,
    policy: NamingPolicy = ONEDRIVE,
    *,
    fallback: str = "untitled",
) -> tuple[str, list[str]]:
    """Return a name the destination will accept, plus why it was altered."""
    reasons: list[str] = []

    cleaned = _CONTROL_RE.sub("", name)
    if cleaned != name:
        reasons.append("removed control characters")
    name = cleaned

    if policy._invalid_re is not None:
        cleaned = policy._invalid_re.sub(REPLACEMENT, name)
        if cleaned != name:
            reasons.append(
                f"replaced characters invalid on {policy.provider} "
                f"({policy.invalid_chars})"
            )
        name = cleaned

    if policy.reserved_prefix or policy.reserved_names or policy.invalid_chars:
        # Leading/trailing whitespace and trailing dots are a Windows rule.
        cleaned = name.strip().rstrip(".").rstrip()
        if cleaned != name:
            reasons.append("trimmed leading/trailing spaces and dots")
        name = cleaned
    else:
        cleaned = name.strip()
        if cleaned != name:
            reasons.append("trimmed leading/trailing spaces")
        name = cleaned

    if policy.reserved_infix and policy.reserved_infix in name.lower():
        name = re.sub(
            re.escape(policy.reserved_infix),
            policy.reserved_infix.rstrip("_") + "-",
            name,
            flags=re.IGNORECASE,
        )
        reasons.append(f"rewrote reserved sequence '{policy.reserved_infix}'")

    if policy.reserved_prefix and name.startswith(policy.reserved_prefix):
        name = REPLACEMENT + name[len(policy.reserved_prefix) :]
        reasons.append(f"rewrote reserved prefix '{policy.reserved_prefix}'")

    if name.lower() in policy.reserved_names:
        name = f"{name}{REPLACEMENT}"
        reasons.append("appended '_' to a reserved device name")

    if not name:
        name = fallback
        reasons.append("name was empty after cleaning")

    if len(name) > policy.max_name_length:
        stem, ext = _split_ext(name)
        keep = policy.max_name_length - len(ext)
        name = stem[: max(keep, 1)] + ext
        reasons.append(f"truncated to {policy.max_name_length} characters")

    return name, reasons


def dedupe_name(
    name: str, taken: set[str], policy: NamingPolicy = ONEDRIVE
) -> tuple[str, bool]:
    """Make ``name`` unique among its siblings, where the destination demands it.

    Mirrors how OneDrive itself renames on conflict: ``report (1).pdf``. On a
    destination that allows duplicates the name is returned untouched, since
    renaming there would invent a difference the user never asked for.
    """
    key = policy.fold(name)
    if not policy.unique_names:
        taken.add(key)
        return name, False

    if key not in taken:
        taken.add(key)
        return name, False

    stem, ext = _split_ext(name)
    counter = 1
    while True:
        candidate = f"{stem} ({counter}){ext}"
        if len(candidate) > policy.max_name_length:
            overflow = len(candidate) - policy.max_name_length
            stem = stem[: max(len(stem) - overflow, 1)]
            candidate = f"{stem} ({counter}){ext}"
        ckey = policy.fold(candidate)
        if ckey not in taken:
            taken.add(ckey)
            return candidate, True
        counter += 1


def check_path_length(path: str, policy: NamingPolicy = ONEDRIVE) -> str | None:
    """Return a warning if the destination path exceeds the provider's limit."""
    if policy.max_path_length is not None and len(path) > policy.max_path_length:
        return (
            f"destination path is {len(path)} characters, over {policy.provider}'s "
            f"{policy.max_path_length}-character limit"
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
