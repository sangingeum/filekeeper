"""Unified-diff application: validation, recovery, per-hunk results and undo."""

from __future__ import annotations

from pathlib import Path

import pytest

from filekeeper.core import operations
from filekeeper.core.diffing import unified_diff_text
from filekeeper.core.errors import ContentConflictError, PatchFailedError, StorageError
from filekeeper.core.safety.hashing import hash_bytes


def _write(root: Path, name: str, content: str) -> Path:
    target = root / name
    target.write_text(content, encoding="utf-8")
    return target


def _patch(path: str, before: str, after: str) -> str:
    fromfile = "/dev/null" if before == "" else f"a/{path}"
    tofile = "/dev/null" if after == "" else f"b/{path}"
    return unified_diff_text(before, after, fromfile=fromfile, tofile=tofile)


def test_a_multi_file_patch_applies_and_reports_every_hunk(service, workspace_root: Path) -> None:
    _write(workspace_root, "a.txt", "one\ntwo\n")
    _write(workspace_root, "b.txt", "red\n")

    patch = _patch("a.txt", "one\ntwo\n", "one\nTWO\n") + _patch("b.txt", "red\n", "blue\n")

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

    patch = _patch("new.txt", "", "created\n") + _patch("gone.txt", "removed\n", "")

    result = service.apply_patch(patch)

    assert (workspace_root / "new.txt").read_text(encoding="utf-8") == "created\n"
    assert not (workspace_root / "gone.txt").exists()
    assert [file.change for file in result.patch_files] == ["created", "deleted"]


def test_undoing_a_multi_file_patch_restores_every_file(service, workspace_root: Path) -> None:
    first = _write(workspace_root, "a.txt", "one\ntwo\n")
    second = _write(workspace_root, "b.txt", "red\n")
    patch = _patch("a.txt", "one\ntwo\n", "one\nTWO\n") + _patch("b.txt", "red\n", "blue\n")

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
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -5,1 +5,1 @@\n-x\n+y\n"

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


def test_a_truncated_hunk_reports_expected_and_actual_counts(service, workspace_root: Path) -> None:
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


def test_a_hunk_matching_only_elsewhere_is_refused_with_its_true_position(
    service, workspace_root: Path
) -> None:
    """A stale hunk header is a refusal, never a relocation.

    The hunk declares line 4 while its context occurs once, at line 7. Applying
    the change there would edit a place the caller never named and report
    success, so the refusal carries the position where the context actually is.
    """

    target = _write(
        workspace_root, "a.txt", "one\ntwo\nthree\nfour\nfive\nsix\nunique-context\neight\n"
    )
    before = target.read_bytes()
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -4,1 +4,1 @@\n-unique-context\n+CHANGED\n"

    with pytest.raises(PatchFailedError) as caught:
        service.apply_patch(patch)

    details = caught.value.details
    assert details["reason"] == "preimage_elsewhere"
    assert details["declared_start"] == 4
    assert details["positions"] == [7]
    assert "line 7" in caught.value.message
    assert target.read_bytes() == before
    assert service.list_history() == ()


def test_a_hunk_that_matches_at_its_declared_line_still_applies(
    service, workspace_root: Path
) -> None:
    target = _write(workspace_root, "a.txt", "one\ntwo\nthree\nfour\n")
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -3,1 +3,1 @@\n-three\n+THREE\n"

    result = service.apply_patch(patch)

    assert [hunk.status for hunk in result.patch_files[0].hunks] == ["applied"]
    assert target.read_text(encoding="utf-8") == "one\ntwo\nTHREE\nfour\n"


def test_a_stale_expected_hash_refuses_the_patch_untouched(
    service, workspace_root: Path
) -> None:
    target = _write(workspace_root, "a.txt", "one\n")
    before = target.read_bytes()
    patch = _patch("a.txt", "one\n", "ONE\n")
    stale = "sha256:" + "0" * 64

    with pytest.raises(ContentConflictError) as caught:
        service.apply_patch(patch, expected_hash=stale)

    details = caught.value.details
    assert details["path"] == "a.txt"
    assert details["expected_hash"] == stale
    assert details["actual_hash"] == hash_bytes(before)
    assert target.read_bytes() == before
    assert service.list_history() == ()


def test_a_matching_expected_hash_applies_the_patch(service, workspace_root: Path) -> None:
    target = _write(workspace_root, "a.txt", "one\n")
    digest = hash_bytes(target.read_bytes())

    result = service.apply_patch(_patch("a.txt", "one\n", "ONE\n"), expected_hash=digest)

    assert result.affected_count == 1
    assert target.read_text(encoding="utf-8") == "ONE\n"


def test_a_later_file_that_cannot_apply_leaves_every_earlier_file_untouched(
    service, workspace_root: Path
) -> None:
    """Planning covers every file before the first write, so file 2 failing is not
    a partial application: file 1 keeps its bytes and no operation is recorded."""

    first = _write(workspace_root, "a.txt", "one\n")
    second = _write(workspace_root, "b.txt", "red\ngreen\n")
    patch = _patch("a.txt", "one\n", "ONE\n") + (
        "--- a/b.txt\n+++ b/b.txt\n@@ -1,1 +1,1 @@\n-absent\n+replaced\n"
    )

    with pytest.raises(PatchFailedError) as caught:
        service.apply_patch(patch)

    assert caught.value.details["reason"] == "preimage_missing"
    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "red\ngreen\n"
    assert service.list_history() == ()
