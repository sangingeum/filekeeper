"""Editing primitives: staged matching, line ranges, guards and undo."""

from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from filekeeper.core.errors import (
    ContentConflictError,
    MatchCountMismatchError,
    NoMatchError,
    ValidationError,
)


def _write(root: Path, name: str, content: str | bytes) -> Path:
    target = root / name
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return target


def test_exact_match_is_reported_as_the_exact_stage(service, workspace_root: Path) -> None:
    _write(workspace_root, "notes.txt", "alpha\nbeta\ngamma\n")

    result = service.replace_text("notes.txt", "beta", "BETA", match="first")

    assert result.match_stage == "exact"
    assert result.match_count == 1
    assert result.operation_id is not None
    assert result.undo_available is True
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_whitespace_normalised_match_is_reported_as_that_stage(
    service, workspace_root: Path
) -> None:
    _write(workspace_root, "code.py", "\tvalue = 1   \n")

    result = service.replace_text("code.py", "    value = 1", "value = 2", match="first")

    assert result.match_stage == "normalised"
    assert result.match_count == 1
    assert (workspace_root / "code.py").read_text(encoding="utf-8") == "\tvalue = 2\n"


def test_no_match_fails_and_names_candidate_lines(service, workspace_root: Path) -> None:
    _write(workspace_root, "notes.txt", "alpha\nbeta\ngamma\n")

    with pytest.raises(NoMatchError) as caught:
        service.replace_text("notes.txt", "delta", "DELTA", match="first")

    assert str(caught.value.code) == "NO_MATCH"
    assert caught.value.details["match_stage"] == "none"
    candidates = caught.value.details["candidates"]
    assert candidates
    assert {candidate["line"] for candidate in candidates} == {1, 2, 3}
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_match_count_mismatch_is_a_refusal(service, workspace_root: Path) -> None:
    _write(workspace_root, "notes.txt", "x\nx\n")

    with pytest.raises(MatchCountMismatchError) as caught:
        service.replace_text("notes.txt", "x", "y", match="all", expected_match_count=1)

    assert caught.value.details["actual_match_count"] == 2
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "x\nx\n"


def test_first_policy_replaces_only_the_first_occurrence(service, workspace_root: Path) -> None:
    _write(workspace_root, "notes.txt", "x\nx\nx\n")

    result = service.replace_text("notes.txt", "x", "y", match="first")

    assert result.match_count == 3
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "y\nx\nx\n"


def test_all_policy_replaces_every_occurrence(service, workspace_root: Path) -> None:
    _write(workspace_root, "notes.txt", "x\nx\nx\n")

    result = service.replace_text("notes.txt", "x", "y", match="all")

    assert result.match_count == 3
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "y\ny\ny\n"


def test_expected_hash_refuses_a_stale_read(service, workspace_root: Path) -> None:
    _write(workspace_root, "notes.txt", "alpha\n")

    with pytest.raises(ContentConflictError):
        service.replace_text(
            "notes.txt",
            "alpha",
            "beta",
            match="first",
            expected_hash="sha256:" + "0" * 64,
        )

    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "alpha\n"


def test_expected_content_guards_the_target_region(service, workspace_root: Path) -> None:
    _write(workspace_root, "notes.txt", "alpha\n")

    with pytest.raises(ContentConflictError):
        service.replace_text(
            "notes.txt",
            "alpha",
            "beta",
            match="first",
            expected_content="something else",
        )


def test_empty_search_text_is_rejected(service, workspace_root: Path) -> None:
    with pytest.raises(ValidationError):
        service.replace_text("notes.txt", "", "x", match="first")


def test_an_unknown_match_policy_is_rejected(service) -> None:
    with pytest.raises(ValidationError):
        service.replace_text("notes.txt", "a", "b", match="sometimes")


def test_replace_text_dry_run_writes_nothing(service, workspace_root: Path) -> None:
    before = (workspace_root / "notes.txt").read_bytes()

    result = service.replace_text("notes.txt", "beta", "BETA", match="first", dry_run=True)

    assert result.dry_run is True
    assert result.operation_id is None
    assert result.preview_diff is not None
    assert "-beta\n" in result.preview_diff
    assert "+BETA\n" in result.preview_diff
    assert all(line.endswith("\n") for line in result.preview_diff.splitlines(keepends=True))
    assert (workspace_root / "notes.txt").read_bytes() == before


def test_replace_text_is_undoable(service, workspace_root: Path) -> None:
    target = _write(workspace_root, "notes.txt", "alpha\nbeta\ngamma\n")
    before = target.read_bytes()

    result = service.replace_text("notes.txt", "beta", "BETA", match="first")
    undone = service.undo(result.operation_id or "")

    assert target.read_bytes() == before
    assert undone.operation_id is not None


def test_replace_lines_replaces_an_inclusive_range(service, workspace_root: Path) -> None:
    _write(workspace_root, "notes.txt", "one\ntwo\nthree\n")

    result = service.replace_lines("notes.txt", 2, 2, "TWO\n")

    assert result.operation_id is not None
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "one\nTWO\nthree\n"


def test_replace_lines_preserves_crlf_and_a_missing_final_newline(
    service, workspace_root: Path
) -> None:
    _write(workspace_root, "windows.txt", b"one\r\ntwo\r\nthree")

    service.replace_lines("windows.txt", 2, 2, "TWO")

    assert (workspace_root / "windows.txt").read_bytes() == b"one\r\nTWO\r\nthree"


def test_replace_lines_preserves_a_utf8_bom(service, workspace_root: Path) -> None:
    _write(workspace_root, "bom.txt", codecs.BOM_UTF8 + b"one\ntwo\n")

    service.replace_lines("bom.txt", 1, 1, "ONE\n")

    assert (workspace_root / "bom.txt").read_bytes() == codecs.BOM_UTF8 + b"ONE\ntwo\n"


def test_replace_lines_rejects_an_out_of_range_reference(service, workspace_root: Path) -> None:
    before = (workspace_root / "notes.txt").read_bytes()

    with pytest.raises(ValidationError):
        service.replace_lines("notes.txt", 2, 99, "x\n")

    assert (workspace_root / "notes.txt").read_bytes() == before


def test_insert_lines_before_and_after(service, workspace_root: Path) -> None:
    _write(workspace_root, "notes.txt", "one\ntwo\n")

    service.insert_lines("notes.txt", 1, "zero\n", position="before")
    service.insert_lines("notes.txt", 2, "middle\n", position="after")

    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "zero\none\nmiddle\ntwo\n"


def test_insert_lines_at_the_end_uses_a_position_not_a_line_number(
    service, workspace_root: Path
) -> None:
    _write(workspace_root, "notes.txt", "one\ntwo")

    service.insert_lines("notes.txt", None, "three\n", position="end")

    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_insert_lines_rejects_a_line_number_with_the_end_position(service) -> None:
    with pytest.raises(ValidationError):
        service.insert_lines("notes.txt", 2, "x\n", position="end")


def test_insert_lines_rejects_an_out_of_range_anchor(service) -> None:
    with pytest.raises(ValidationError):
        service.insert_lines("notes.txt", 99, "x\n", position="after")


def test_delete_lines_removes_an_inclusive_range(service, workspace_root: Path) -> None:
    _write(workspace_root, "notes.txt", "one\ntwo\nthree\n")

    service.delete_lines("notes.txt", 2, 2)

    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "one\nthree\n"


def test_delete_lines_rejects_an_out_of_range_reference(service) -> None:
    with pytest.raises(ValidationError):
        service.delete_lines("notes.txt", 2, 9)


def test_line_edit_dry_run_writes_nothing(service, workspace_root: Path) -> None:
    before = (workspace_root / "notes.txt").read_bytes()

    result = service.delete_lines("notes.txt", 1, 1, dry_run=True)

    assert result.operation_id is None
    assert result.preview_diff is not None
    assert (workspace_root / "notes.txt").read_bytes() == before


def test_line_edit_is_undoable(service, workspace_root: Path) -> None:
    target = _write(workspace_root, "notes.txt", "one\ntwo\nthree\n")
    before = target.read_bytes()

    result = service.replace_lines("notes.txt", 2, 3, "two\n")
    service.undo(result.operation_id or "")

    assert target.read_bytes() == before


def test_line_edit_honours_expected_hash(service, workspace_root: Path) -> None:
    with pytest.raises(ContentConflictError):
        service.delete_lines("notes.txt", 1, 1, expected_hash="sha256:" + "1" * 64)
