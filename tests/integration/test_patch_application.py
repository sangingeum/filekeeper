"""Unified-diff application: validation, recovery, per-hunk results and undo."""

from __future__ import annotations

from pathlib import Path

import pytest

from filekeeper.core import operations
from filekeeper.core.diffing import unified_diff_text
from filekeeper.core.errors import PatchFailedError, StorageError


def _write(root: Path, name: str, content: str) -> Path:
    target = root / name
    target.write_text(content, encoding="utf-8")
    return target


def _patch(path: str, before: str, after: str) -> str:
    fromfile = "/dev/null" if before == "" else f"a/{path}"
    tofile = "/dev/null" if after == "" else f"b/{path}"
    return unified_diff_text(before, after, fromfile=fromfile, tofile=tofile)


def test_a_multi_file_patch_applies_and_reports_every_hunk(
    service, workspace_root: Path
) -> None:
    _write(workspace_root, "a.txt", "one\ntwo\n")
    _write(workspace_root, "b.txt", "red\n")

    patch = _patch("a.txt", "one\ntwo\n", "one\nTWO\n") + _patch(
        "b.txt", "red\n", "blue\n"
    )

    result = service.apply_patch(patch)

    assert result.operation_id is not None
    assert result.affected_count == 2
    assert [file.path for file in result.patch_files] == ["a.txt", "b.txt"]
    for file in result.patch_files:
        assert file.applied is True
        assert [hunk.status for hunk in file.hunks] == ["applied"]
    assert (workspace_root / "a.txt").read_text(encoding="utf-8") == "one\nTWO\n"
    assert (workspace_root / "b.txt").read_text(encoding="utf-8") == "blue\n"
    assert any("recoverable" in warning for warning in result.warnings)


def test_a_patch_can_create_and_delete_files(service, workspace_root: Path) -> None:
    _write(workspace_root, "gone.txt", "removed\n")

    patch = _patch("new.txt", "", "created\n") + _patch(
        "gone.txt", "removed\n", ""
    )

    result = service.apply_patch(patch)

    assert (workspace_root / "new.txt").read_text(encoding="utf-8") == "created\n"
    assert not (workspace_root / "gone.txt").exists()
    assert [file.change for file in result.patch_files] == ["created", "deleted"]


def test_undoing_a_multi_file_patch_restores_every_file(
    service, workspace_root: Path
) -> None:
    first = _write(workspace_root, "a.txt", "one\ntwo\n")
    second = _write(workspace_root, "b.txt", "red\n")
    patch = _patch("a.txt", "one\ntwo\n", "one\nTWO\n") + _patch(
        "b.txt", "red\n", "blue\n"
    )

    result = service.apply_patch(patch)
    service.undo(result.operation_id or "")

    assert first.read_text(encoding="utf-8") == "one\ntwo\n"
    assert second.read_text(encoding="utf-8") == "red\n"


def test_a_failed_hunk_leaves_the_tree_unmodified(service, workspace_root: Path) -> None:
    target = _write(workspace_root, "a.txt", "one\ntwo\n")
    before = target.read_bytes()
    patch = _patch("a.txt", "absent\n", "present\n")

    with pytest.raises(PatchFailedError) as caught:
        service.apply_patch(patch)

    assert caught.value.details["reason"] == "preimage_missing"
    assert target.read_bytes() == before
    assert service.list_history() == ()


def test_an_ambiguous_hunk_is_rejected(service, workspace_root: Path) -> None:
    target = _write(workspace_root, "a.txt", "x\nx\n")
    before = target.read_bytes()
    patch = (
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -5,1 +5,1 @@\n"
        "-x\n"
        "+y\n"
    )

    with pytest.raises(PatchFailedError) as caught:
        service.apply_patch(patch)

    assert caught.value.details["reason"] == "ambiguous"
    assert target.read_bytes() == before


def test_a_partial_application_is_rolled_back_to_the_pre_patch_state(
    service, workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _write(workspace_root, "a.txt", "one\n")
    second = _write(workspace_root, "b.txt", "two\n")
    patch = _patch("a.txt", "one\n", "ONE\n") + _patch("b.txt", "two\n", "TWO\n")

    original = operations.edit.atomic_write_bytes
    calls = {"count": 0}

    def flaky(path, data, **kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 2:
            raise StorageError("injected write failure")
        return original(path, data, **kwargs)

    monkeypatch.setattr(operations.edit, "atomic_write_bytes", flaky)

    with pytest.raises(PatchFailedError) as caught:
        service.apply_patch(patch)

    assert sorted(caught.value.details["rolled_back"]) == ["a.txt", "b.txt"]
    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "two\n"


def test_patch_dry_run_writes_nothing(service, workspace_root: Path) -> None:
    target = _write(workspace_root, "a.txt", "one\n")
    before = target.read_bytes()
    patch = _patch("a.txt", "one\n", "ONE\n")

    result = service.apply_patch(patch, dry_run=True)

    assert result.operation_id is None
    assert result.dry_run is True
    assert result.preview_diff == patch
    assert result.patch_files[0].hunks[0].status == "would_apply"
    assert target.read_bytes() == before


def test_a_truncated_hunk_reports_expected_and_actual_counts(
    service, workspace_root: Path
) -> None:
    """A short hunk states the header's counts and what the body actually had.

    The caller can locate and repair the patch in one step instead of asking
    for another diagnostic round trip. The existing ``line`` and ``reason``
    fields stay, and no patch text is echoed into the message.
    """

    target = _write(workspace_root, "a.txt", "one\ntwo\nthree\nfour\nfive\n")
    before = target.read_bytes()
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1,5 +1,5 @@\n-one\n"

    with pytest.raises(PatchFailedError) as caught:
        service.apply_patch(patch)

    details = caught.value.details
    assert details["reason"] == "syntax"
    assert details["line"] == 5
    assert details["expected_old_lines"] == 5
    assert details["expected_new_lines"] == 5
    assert details["actual_old_lines"] == 1
    assert details["actual_new_lines"] == 0
    message = caught.value.message
    assert "5 old" in message and "5 new" in message
    assert "1 old" in message and "0 new" in message
    assert "-one" not in message
    assert target.read_bytes() == before


def test_a_hunk_that_ends_early_reports_its_counts(service, workspace_root: Path) -> None:
    """The end-of-patch truncation path reports counts too, not only the prefix path."""

    target = _write(workspace_root, "a.txt", "one\ntwo\nthree\nfour\nfive\n")
    before = target.read_bytes()
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1,5 +1,5 @@\n-one\n-two"

    with pytest.raises(PatchFailedError) as caught:
        service.apply_patch(patch)

    details = caught.value.details
    assert details["reason"] == "syntax"
    assert "line counts do not match" in caught.value.message
    assert details["expected_old_lines"] == 5
    assert details["expected_new_lines"] == 5
    assert details["actual_old_lines"] == 2
    assert details["actual_new_lines"] == 0
    assert target.read_bytes() == before


def test_an_invalid_patch_is_rejected_before_anything_is_touched(
    service, workspace_root: Path
) -> None:
    target = _write(workspace_root, "a.txt", "one\n")
    before = target.read_bytes()

    with pytest.raises(PatchFailedError) as caught:
        service.apply_patch("-one\n+two\n")

    assert caught.value.details["reason"] == "syntax"
    assert target.read_bytes() == before
