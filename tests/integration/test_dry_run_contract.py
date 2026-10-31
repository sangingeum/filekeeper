"""The dry-run contract: a preview never claims a mutation happened.

A dry run observes nothing, so its result must not look like a completed write.
``changed`` is ``False``, ``operation_id`` is ``None``, and ``after_hash`` is
``None`` on the result and on every nested item and patch file. The predicted
digest stays available under ``predicted_after_hash``. A real run keeps
reporting ``changed=True`` with its observed ``after_hash``.
"""

from __future__ import annotations

from pathlib import Path

from filekeeper.core.diffing import unified_diff_text
from filekeeper.core.models import MutationResult


def _assert_preview_contract(result: MutationResult) -> None:
    """Every dry run reports the same honest shape."""

    assert result.dry_run is True
    assert result.changed is False
    assert result.operation_id is None
    assert result.after_hash is None
    for item in result.items:
        assert item.after_hash is None
        assert item.size_bytes is None
    for patch_file in result.patch_files:
        assert patch_file.applied is False
        assert patch_file.after_hash is None


def _patch(path: str, before: str, after: str) -> str:
    fromfile = "/dev/null" if before == "" else f"a/{path}"
    tofile = "/dev/null" if after == "" else f"b/{path}"
    return unified_diff_text(before, after, fromfile=fromfile, tofile=tofile)


def test_dry_run_create_reports_no_change_and_no_observed_hash(
    service, workspace_root: Path
) -> None:
    result = service.create_file("new.txt", "hello\n", dry_run=True)

    _assert_preview_contract(result)
    assert result.predicted_after_hash is not None
    assert not (workspace_root / "new.txt").exists()


def test_dry_run_write_reports_no_change_and_predicts_the_hash(
    service, workspace_root: Path
) -> None:
    result = service.write_file("notes.txt", "two\n", dry_run=True)

    _assert_preview_contract(result)
    assert result.predicted_after_hash is not None
    assert result.predicted_after_hash != result.before_hash
    assert result.would_change is True
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == (
        "alpha\nbeta\ngamma\n"
    )


def test_dry_run_write_reports_would_change_false_for_identical_content(
    service,
) -> None:
    result = service.write_file("notes.txt", "alpha\nbeta\ngamma\n", dry_run=True)

    _assert_preview_contract(result)
    assert result.would_change is False
    assert result.predicted_after_hash == result.before_hash


def test_dry_run_identical_content_never_claims_an_item_change(service) -> None:
    """A preview whose operation-level verdict is "nothing" must agree with itself.

    ``would_change`` is false for identical content, so no item in the same
    payload may report that it would be modified: an agent reading the item list
    would otherwise act on a change that will not happen.
    """

    result = service.write_file("notes.txt", "alpha\nbeta\ngamma\n", dry_run=True)

    assert result.would_change is False
    assert result.changed is False
    assert result.items, "the preview still names the path it inspected"
    for item in result.items:
        assert item.change == "unchanged"
        assert item.predicted_after_hash == result.predicted_after_hash


def test_a_would_change_preview_still_names_the_item_change(service) -> None:
    """The consistency rule only applies when nothing would change."""

    result = service.write_file("notes.txt", "different\n", dry_run=True)

    assert result.would_change is True
    assert [item.change for item in result.items] == ["modified"]


def test_dry_run_replace_text_reports_no_change_and_keeps_its_preview(
    service, workspace_root: Path
) -> None:
    result = service.replace_text(
        "notes.txt", "beta", "BETA", match="first", dry_run=True
    )

    _assert_preview_contract(result)
    assert result.preview_diff is not None
    assert result.predicted_after_hash is not None
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == (
        "alpha\nbeta\ngamma\n"
    )


def test_dry_run_replace_lines_reports_no_change(service, workspace_root: Path) -> None:
    result = service.replace_lines("notes.txt", 2, 2, "BETA\n", dry_run=True)

    _assert_preview_contract(result)
    assert result.preview_diff is not None
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == (
        "alpha\nbeta\ngamma\n"
    )


def test_dry_run_insert_lines_reports_no_change(service, workspace_root: Path) -> None:
    result = service.insert_lines("notes.txt", 1, "inserted\n", dry_run=True)

    _assert_preview_contract(result)
    assert result.preview_diff is not None
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == (
        "alpha\nbeta\ngamma\n"
    )


def test_dry_run_delete_lines_reports_no_change(service, workspace_root: Path) -> None:
    result = service.delete_lines("notes.txt", 1, 1, dry_run=True)

    _assert_preview_contract(result)
    assert result.preview_diff is not None
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == (
        "alpha\nbeta\ngamma\n"
    )


def test_dry_run_apply_patch_reports_no_change_and_keeps_its_preview(
    service, workspace_root: Path
) -> None:
    target = workspace_root / "notes.txt"
    patch = _patch("notes.txt", "alpha\nbeta\ngamma\n", "alpha\nBETA\ngamma\n")

    result = service.apply_patch(patch, dry_run=True)

    _assert_preview_contract(result)
    assert result.preview_diff == patch
    assert result.patch_files[0].hunks[0].status == "would_apply"
    assert result.predicted_after_hash is not None
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_dry_run_copy_reports_no_change(service, workspace_root: Path) -> None:
    result = service.copy_file("notes.txt", "copy.txt", dry_run=True)

    _assert_preview_contract(result)
    assert result.predicted_after_hash is not None
    assert not (workspace_root / "copy.txt").exists()


def test_dry_run_move_reports_no_change(service, workspace_root: Path) -> None:
    result = service.move_file("notes.txt", "moved.txt", dry_run=True)

    _assert_preview_contract(result)
    assert (workspace_root / "notes.txt").exists()
    assert not (workspace_root / "moved.txt").exists()


def test_dry_run_delete_file_reports_no_change(service, workspace_root: Path) -> None:
    result = service.delete_file("notes.txt", dry_run=True)

    _assert_preview_contract(result)
    assert result.before_hash is not None
    assert (workspace_root / "notes.txt").exists()


def test_dry_run_create_directory_reports_no_change(
    service, workspace_root: Path
) -> None:
    result = service.create_directory("fresh", dry_run=True)

    _assert_preview_contract(result)
    assert not (workspace_root / "fresh").exists()


def test_dry_run_move_directory_reports_no_change(
    service, workspace_root: Path
) -> None:
    result = service.move_directory("sub", "renamed", dry_run=True)

    _assert_preview_contract(result)
    assert (workspace_root / "sub").is_dir()
    assert not (workspace_root / "renamed").exists()


def test_dry_run_delete_directory_reports_no_change(
    service, workspace_root: Path
) -> None:
    result = service.delete_directory(
        "sub", recursive=True, confirm_recursive=True, dry_run=True
    )

    _assert_preview_contract(result)
    assert (workspace_root / "sub" / "nested.md").is_file()


def test_real_write_still_reports_change_and_its_after_hash(service) -> None:
    result = service.write_file("notes.txt", "two\n")
    payload = result.to_dict()

    assert result.dry_run is False
    assert result.changed is True
    assert result.after_hash is not None
    assert payload.get("predicted_after_hash") is None
    assert result.operation_id is not None
    assert result.items[0].after_hash == result.after_hash
    assert result.items[0].size_bytes == 4


def test_real_patch_still_reports_change_and_its_after_hash(
    service, workspace_root: Path
) -> None:
    patch = _patch("notes.txt", "alpha\nbeta\ngamma\n", "alpha\nBETA\ngamma\n")

    result = service.apply_patch(patch)
    payload = result.to_dict()

    assert result.dry_run is False
    assert result.changed is True
    assert result.after_hash is not None
    assert payload.get("predicted_after_hash") is None
    assert result.patch_files[0].applied is True
    assert result.patch_files[0].after_hash == result.after_hash


def test_dry_run_payload_has_no_observed_result_fields(service) -> None:
    payload = service.write_file("notes.txt", "two\n", dry_run=True).to_dict()

    assert payload["changed"] is False
    assert payload["operation_id"] is None
    assert "after_hash" not in payload or payload["after_hash"] is None
    assert payload["would_change"] is True
    assert payload["predicted_after_hash"] is not None
    for item in payload["items"]:
        assert item.get("after_hash") is None
        assert item.get("size_bytes") is None
