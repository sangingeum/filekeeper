"""Editing API is published but not implemented: it must raise, never no-op."""

from __future__ import annotations

from pathlib import Path

import pytest

from filekeeper.core import operations
from filekeeper.core.errors import NotImplementedYetError


def test_replace_text_raises_and_leaves_the_file_untouched(service, workspace_root: Path) -> None:
    target = workspace_root / "notes.txt"
    before = target.read_bytes()

    with pytest.raises(NotImplementedError):
        operations.edit.replace_text(service.handle, "notes.txt", "beta", "BETA")

    assert target.read_bytes() == before


def test_line_editing_stubs_raise(service) -> None:
    handle = service.handle
    with pytest.raises(NotImplementedYetError):
        operations.edit.replace_lines(handle, "notes.txt", 1, 2, "x\n")
    with pytest.raises(NotImplementedYetError):
        operations.edit.insert_lines(handle, "notes.txt", 1, "x\n")
    with pytest.raises(NotImplementedYetError):
        operations.edit.delete_lines(handle, "notes.txt", 1, 1)
    with pytest.raises(NotImplementedYetError):
        operations.edit.apply_patch(handle, "")


def test_stub_error_is_a_filekeeper_error_with_a_code() -> None:
    error = NotImplementedYetError("later")
    assert str(error.code) == "NOT_IMPLEMENTED_YET"
    assert isinstance(error, NotImplementedError)
