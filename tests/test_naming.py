"""Name translation from Drive's rules to OneDrive's."""

from __future__ import annotations

import pytest

from cloudtransfer.naming import (
    INVALID_CHARS,
    MAX_NAME_LENGTH,
    check_path_length,
    dedupe_name,
    join_path,
    sanitize_name,
)


@pytest.mark.parametrize("char", list(INVALID_CHARS))
def test_every_invalid_character_is_replaced(char):
    name, reasons = sanitize_name(f"report{char}final.pdf")
    assert char not in name
    assert name == "report_final.pdf"
    assert reasons


def test_clean_names_are_left_alone():
    name, reasons = sanitize_name("Quarterly report 2026.xlsx")
    assert name == "Quarterly report 2026.xlsx"
    assert reasons == []


def test_trailing_dots_and_spaces_are_trimmed():
    name, reasons = sanitize_name("  notes...  ")
    assert name == "notes"
    assert any("trimmed" in r for r in reasons)


def test_reserved_device_names_get_a_suffix():
    for reserved in ("CON", "nul", "LPT1", ".lock", "desktop.ini"):
        name, reasons = sanitize_name(reserved)
        assert name != reserved
        assert name.endswith("_")
        assert any("reserved" in r for r in reasons)


def test_reserved_prefix_and_infix_are_rewritten():
    name, _ = sanitize_name("~$draft.docx")
    assert not name.startswith("~$")

    name, reasons = sanitize_name("my_vti_file.txt")
    assert "_vti_" not in name.lower()
    assert any("_vti_" in r for r in reasons)


def test_control_characters_are_stripped():
    name, reasons = sanitize_name("line\x00break\x1f.txt")
    assert name == "linebreak.txt"
    assert any("control" in r for r in reasons)


def test_empty_name_falls_back():
    name, reasons = sanitize_name('***', fallback="untitled")
    # Three invalid characters become three underscores, which is a valid name.
    assert name == "___"

    name, reasons = sanitize_name("   ", fallback="untitled")
    assert name == "untitled"
    assert any("empty" in r for r in reasons)


def test_long_names_are_truncated_but_keep_the_extension():
    name, reasons = sanitize_name("a" * 300 + ".pdf")
    assert len(name) == MAX_NAME_LENGTH
    assert name.endswith(".pdf")
    assert any("truncated" in r for r in reasons)


def test_dedupe_keeps_both_files():
    """Drive allows identical sibling names; OneDrive does not."""
    taken: set[str] = set()
    first, renamed_first = dedupe_name("photo.jpg", taken)
    second, renamed_second = dedupe_name("photo.jpg", taken)
    third, _ = dedupe_name("photo.jpg", taken)

    assert first == "photo.jpg" and not renamed_first
    assert second == "photo (1).jpg" and renamed_second
    assert third == "photo (2).jpg"


def test_dedupe_is_case_insensitive():
    """OneDrive treats Report.pdf and report.pdf as the same name."""
    taken: set[str] = set()
    dedupe_name("Report.pdf", taken)
    second, renamed = dedupe_name("report.pdf", taken)
    assert renamed
    assert second == "report (1).pdf"


def test_dedupe_handles_names_without_an_extension():
    taken: set[str] = set()
    dedupe_name("README", taken)
    assert dedupe_name("README", taken)[0] == "README (1)"


def test_dedupe_respects_the_length_cap():
    taken = {("x" * MAX_NAME_LENGTH).casefold()}
    result, renamed = dedupe_name("x" * MAX_NAME_LENGTH, taken)
    assert renamed
    assert len(result) <= MAX_NAME_LENGTH


def test_sanitizing_can_collide_two_distinct_names():
    """'a:b' and 'a*b' both sanitize to 'a_b'; dedupe has to catch that."""
    taken: set[str] = set()
    first, _ = sanitize_name("a:b.txt")
    first, _ = dedupe_name(first, taken)
    second, _ = sanitize_name("a*b.txt")
    second, renamed = dedupe_name(second, taken)
    assert first == "a_b.txt"
    assert second == "a_b (1).txt"
    assert renamed


def test_path_length_warning():
    assert check_path_length("short/path.txt") is None
    warning = check_path_length("x" * 401)
    assert warning is not None and "400" in warning


def test_join_path_skips_empty_segments():
    assert join_path("", "folder", "", "file.txt") == "folder/file.txt"
    assert join_path("") == ""
    assert join_path("a/", "/b") == "a/b"
