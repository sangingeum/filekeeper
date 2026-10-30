"""Reads, ranges, truncation, metadata, listing and search."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from filekeeper.core.errors import (
    BinaryContentRefusedError,
    InvalidPatternError,
    ValidationError,
)


def test_reads_whole_file(service) -> None:
    result = service.read_file("notes.txt")
    assert result.content == "alpha\nbeta\ngamma\n"
    assert result.total_lines == 3
    assert result.start_line == 1
    assert result.end_line == 3
    assert result.returned_line_count == 3
    assert result.truncated is False
    assert result.encoding == "utf-8"
    assert result.newline == "\n"


def test_line_ranges_are_one_based_and_inclusive(service) -> None:
    middle = service.read_file("notes.txt", start_line=2, end_line=2)
    assert middle.content == "beta\n"
    assert (middle.start_line, middle.end_line) == (2, 2)

    tail = service.read_file("notes.txt", start_line=2, end_line=3)
    assert tail.content == "beta\ngamma\n"
    assert tail.returned_line_count == 2


def test_out_of_range_ranges_are_rejected(service) -> None:
    with pytest.raises(ValidationError):
        service.read_file("notes.txt", start_line=0)
    with pytest.raises(ValidationError):
        service.read_file("notes.txt", start_line=3, end_line=2)
    with pytest.raises(ValidationError):
        service.read_file("notes.txt", start_line=1, end_line=9)
    with pytest.raises(ValidationError):
        service.read_file("notes.txt", start_line=7)


def test_end_line_requires_start_line(service) -> None:
    with pytest.raises(ValidationError):
        service.read_file("notes.txt", end_line=2)


def test_empty_file_reads_as_zero_lines(service) -> None:
    target = service.root / "empty.txt"
    target.write_text("", encoding="utf-8")

    result = service.read_file("empty.txt")
    assert result.content == ""
    assert result.total_lines == 0
    assert result.returned_line_count == 0


def test_bounded_read_reports_truncation(small_limits) -> None:
    result = small_limits.read_file("notes.txt")
    assert result.truncated is True
    assert result.total_lines is None
    assert result.next_start_line is not None
    assert result.truncation_reason is not None
    assert len(result.content) <= 12


def test_explicit_max_bytes_cannot_exceed_the_safety_limit(small_limits) -> None:
    result = small_limits.read_file("notes.txt", max_bytes=4096)
    assert result.truncated is True


def test_binary_content_is_refused(service) -> None:
    (service.root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    with pytest.raises(BinaryContentRefusedError):
        service.read_file("image.png")


def test_read_multiple_files_returns_failures_as_data(service) -> None:
    entries = service.read_multiple_files(["notes.txt", "absent.txt"])
    assert entries[0].ok is True
    assert entries[0].result is not None
    assert entries[1].ok is False
    assert entries[1].error is not None
    assert entries[1].error.code == "PATH_NOT_FOUND"


def test_get_file_info_reports_text_characteristics(service) -> None:
    info = service.get_file_info("notes.txt")
    expected_hash = "sha256:" + hashlib.sha256(b"alpha\nbeta\ngamma\n").hexdigest()

    assert info.kind == "file"
    assert info.size_bytes == 17
    assert info.is_text is True
    assert info.line_count == 3
    assert info.encoding == "utf-8"
    assert info.newline == "\n"
    assert info.content_hash == expected_hash


def test_get_file_info_reports_symlinks_without_following_them(service) -> None:
    os.symlink(service.root / "notes.txt", service.root / "alias.txt")
    info = service.get_file_info("alias.txt")

    assert info.kind == "symlink"
    assert info.is_symlink is True
    assert info.symlink_target == str(service.root / "notes.txt")


def test_hash_and_exists(service) -> None:
    digest = service.get_file_hash("notes.txt")
    assert digest.startswith("sha256:")
    assert service.file_exists("notes.txt").exists is True
    assert service.file_exists("absent.txt").exists is False


def test_directory_listing_is_flat_by_default(service) -> None:
    listing = service.list_directory(".")
    names = {entry.name for entry in listing.entries}
    assert names == {"notes.txt", "sub"}
    assert listing.truncated is False


def test_recursive_listing_filters_by_glob(service) -> None:
    listing = service.list_directory(".", recursive=True, glob="*.md")
    assert [entry.path for entry in listing.entries] == ["sub/nested.md"]


def test_listing_reports_the_entry_cap(small_limits) -> None:
    (small_limits.root / "third.txt").write_text("third\n", encoding="utf-8")
    listing = small_limits.list_directory(".")
    assert listing.truncated is True
    assert len(listing.entries) == 2


def test_state_directory_is_hidden_from_listings(service) -> None:
    service.state_database_path()
    listing = service.list_directory(".")
    assert ".filekeeper" not in {entry.name for entry in listing.entries}


def test_path_search_finds_files_and_reports_truncation(service) -> None:
    result = service.search_files(".", "*.txt")
    assert result.paths == ("notes.txt",)
    assert result.truncated is False

    truncated = service.search_files(".", "*", max_results=1)
    assert len(truncated.paths) == 1
    assert truncated.truncated is True


def test_path_search_matches_a_relative_path_pattern(service) -> None:
    result = service.search_files(".", "sub/*.md")
    assert result.paths == ("sub/nested.md",)


def test_content_search_is_literal_by_default(service) -> None:
    result = service.search_content(".", "beta")
    assert len(result.matches) == 1
    assert result.matches[0].path == "notes.txt"
    assert result.matches[0].line_number == 2
    assert result.matches[0].column == 1
    assert result.files_searched >= 1


def test_content_search_can_use_a_regular_expression(service) -> None:
    result = service.search_content(".", r"b.ta", regex=True)
    assert [match.line_number for match in result.matches] == [2]


def test_content_search_is_case_sensitive_unless_asked(service) -> None:
    assert service.search_content(".", "BETA").matches == ()
    assert len(service.search_content(".", "BETA", case_sensitive=False).matches) == 1


def test_content_search_caps_results(service) -> None:
    result = service.search_content(".", "a", max_results=1)
    assert len(result.matches) == 1
    assert result.truncated is True


def test_invalid_regular_expression_is_rejected(service) -> None:
    with pytest.raises(InvalidPatternError):
        service.search_content(".", "([unclosed", regex=True)


def test_content_search_skips_binary_files(service) -> None:
    (service.root / "blob.bin").write_bytes(b"\x00\x01\x02")
    result = service.search_content(".", "alpha")
    assert all(match.path != "blob.bin" for match in result.matches)


def test_reading_outside_the_workspace_is_refused(service, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    with pytest.raises(Exception) as caught:
        service.read_file(str(outside))
    assert "PATH_OUTSIDE_WORKSPACE" in str(caught.value)
