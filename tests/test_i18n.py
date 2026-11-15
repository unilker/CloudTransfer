"""The interface's two languages, kept in step.

A missing translation is invisible in testing - t() quietly falls back to
English - so the only way to catch one is to compare the key sets directly,
and to check that every key the markup and the script ask for actually exists.
"""

from __future__ import annotations

import pathlib
import re

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "cloudtransfer" / "web" / "static"
TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "cloudtransfer" / "web" / "templates"

I18N_JS = (STATIC / "i18n.js").read_text(encoding="utf-8")
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (TEMPLATES / "index.html").read_text(encoding="utf-8")

LANGUAGES = ("en", "tr")


def _block(language: str) -> str:
    """The body of one language's dictionary."""
    start = I18N_JS.index(f"\n  {language}: {{")
    end = I18N_JS.index("\n  },", start)
    return I18N_JS[start:end]


def _keys(language: str) -> set[str]:
    return set(re.findall(r"^\s*'([^']+)':", _block(language), re.MULTILINE))


def _values(language: str) -> dict[str, str]:
    pairs = re.findall(r"^\s*'([^']+)':\s*'(.*)',\s*$", _block(language), re.MULTILINE)
    return dict(pairs)


@pytest.fixture(scope="module")
def keys() -> dict[str, set[str]]:
    return {language: _keys(language) for language in LANGUAGES}


def test_both_dictionaries_are_found(keys):
    for language in LANGUAGES:
        assert len(keys[language]) > 80, f"{language} looks truncated"


def test_the_languages_define_exactly_the_same_keys(keys):
    missing_in_tr = keys["en"] - keys["tr"]
    missing_in_en = keys["tr"] - keys["en"]
    assert not missing_in_tr, f"untranslated into Turkish: {sorted(missing_in_tr)}"
    assert not missing_in_en, f"present only in Turkish: {sorted(missing_in_en)}"


# Strings that are the same in both languages on purpose: they are literal
# paths and permission names as they appear in the provider's own interface,
# so translating them would stop the user finding them on the page.
DELIBERATELY_IDENTICAL = {"ms.step4_html"}


def test_no_turkish_value_is_left_as_the_english_one(keys):
    """A copied-over English string is a translation nobody did."""
    english, turkish = _values("en"), _values("tr")
    shared = set(english) & set(turkish) - DELIBERATELY_IDENTICAL
    identical = {
        key for key in shared
        if english[key] == turkish[key] and len(english[key]) > 24
    }
    assert not identical, f"identical in both languages: {sorted(identical)}"


def test_every_key_the_markup_asks_for_exists(keys):
    used = set(re.findall(r'data-i18n(?:-html)?="([^"]+)"', INDEX_HTML))
    for pair in re.findall(r'data-i18n-attr="([^"]+)"', INDEX_HTML):
        for entry in pair.split(","):
            _, _, key = entry.partition(":")
            if key.strip():
                used.add(key.strip())

    assert used, "the template defines no translatable nodes"
    for language in LANGUAGES:
        unknown = used - keys[language]
        assert not unknown, f"{language} is missing keys the page uses: {sorted(unknown)}"


def test_every_key_the_script_asks_for_exists(keys):
    """Catches a typo in a key, which would otherwise render as the key itself."""
    # Read the arguments of every t(...) call, then take the quoted strings
    # from them. Unlike matching only the first argument this also sees keys
    # chosen by a ternary, and unlike scanning the whole file it does not
    # mistake a CSS selector such as 'li.folder' for a key.
    used: set[str] = set()
    for args in re.findall(r"\bt\(([^()]*)\)", APP_JS):
        # A dot is what distinguishes a key from a comparison literal such as
        # t(role === 'source' ? ... : ...). Every key is namespaced for exactly
        # this reason, which the next test enforces.
        used.update(k for k in re.findall(r"'([^']+)'", args) if "." in k)
    assert len(used) > 40, "no keys found; has the script changed shape?"
    for language in LANGUAGES:
        unknown = used - keys[language]
        assert not unknown, f"{language} is missing keys the script uses: {sorted(unknown)}"


def test_nothing_is_defined_but_never_used(keys):
    """An unused key is usually a rename that left the old one behind."""
    # Substring search rather than a call-shaped regex: a key may be picked by
    # a ternary inside t(), which no argument pattern would see.
    used = {key for key in keys["en"] if f"'{key}'" in APP_JS}
    used |= set(re.findall(r'data-i18n(?:-html)?="([^"]+)"', INDEX_HTML))

    # Some keys are built at runtime, as t(`status.${job.status}`). Treat every
    # key under such a prefix as used, since the whole family is reachable.
    for prefix in re.findall(r"t\(`([a-z][a-zA-Z.]*\.)\$\{", APP_JS):
        used |= {key for key in keys["en"] if key.startswith(prefix)}
    for pair in re.findall(r'data-i18n-attr="([^"]+)"', INDEX_HTML):
        for entry in pair.split(","):
            _, _, key = entry.partition(":")
            if key.strip():
                used.add(key.strip())

    orphans = keys["en"] - used
    assert not orphans, f"defined but never used: {sorted(orphans)}"


def test_placeholders_match_between_languages():
    """{name} tokens must survive translation, or a value renders as literal text."""
    english, turkish = _values("en"), _values("tr")
    for key in set(english) & set(turkish):
        in_en = set(re.findall(r"\{(\w+)\}", english[key]))
        in_tr = set(re.findall(r"\{(\w+)\}", turkish[key]))
        assert in_en == in_tr, f"{key}: placeholders {in_en} vs {in_tr}"


def test_markup_keys_are_marked_as_html():
    """A key holding tags must be written with innerHTML, never textContent."""
    html_keys = {
        key for key in _keys("en")
        if key.endswith("_html")
    }
    for key in html_keys:
        pattern = f'data-i18n="{key}"'
        assert pattern not in INDEX_HTML, (
            f"{key} contains markup but is bound with data-i18n; use data-i18n-html"
        )

    # And the reverse: anything bound as HTML should be named that way, so the
    # distinction stays visible when someone edits the dictionary.
    for key in re.findall(r'data-i18n-html="([^"]+)"', INDEX_HTML):
        assert key.endswith("_html"), f"{key} is bound as HTML but not named _html"


def test_the_page_loads_the_translations_before_the_application():
    """app.js calls t() at load time; i18n.js has to be in place already."""
    i18n_at = INDEX_HTML.index("/static/i18n.js")
    app_at = INDEX_HTML.index("/static/app.js")
    assert i18n_at < app_at


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_setup_step_is_fully_translated(language, keys):
    """The step people get stuck on; every part of it must be covered."""
    required = {
        "setup.heading",
        "setup.lede_html",
        "google.step1_html",
        "google.step5_html",
        "ms.step1_html",
        "ms.step5_html",
        "import.label",
        "import.button",
        "import.hint",
        "import.ok",
        "import.bad",
    }
    assert required <= keys[language]


def test_every_key_is_namespaced(keys):
    """A dot in every key is what lets the extraction above tell a key from a
    comparison literal. Adding a bare one would silently weaken that test."""
    for language in LANGUAGES:
        flat = {key for key in keys[language] if "." not in key}
        assert not flat, f"{language} has un-namespaced keys: {sorted(flat)}"


def test_every_job_status_has_a_translation(keys):
    """Statuses are looked up by a key built at runtime, so a missing one shows
    the raw value from the database instead of a sentence."""
    from cloudtransfer.models import JobStatus

    statuses = {
        value for name, value in vars(JobStatus).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    for language in LANGUAGES:
        missing = {s for s in statuses if f"status.{s}" not in keys[language]}
        assert not missing, f"{language} has no label for: {sorted(missing)}"


def test_every_direction_has_a_translation(keys):
    from cloudtransfer.models import Direction

    for language in LANGUAGES:
        missing = {d for d in Direction.ALL if f"direction.{d}" not in keys[language]}
        assert not missing, f"{language} has no label for: {sorted(missing)}"
