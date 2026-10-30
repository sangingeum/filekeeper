"""Reserved surface for text editing. Not implemented in this release.

The signatures are published now so that the module layout and the API shape are
fixed: exact-text replacement and inclusive line-range editing land in a later
release, and adding them must not rearrange anything a caller already imports.
Every function raises :class:`NotImplementedYetError` (a ``NotImplementedError``
subclass). Nothing here is a silent no-op, and nothing here touches the
filesystem.

When implemented, the matching policy is staged, per the design review:

1. exact literal match — reported as ``"exact"``;
2. whitespace-normalised match — reported as ``"normalised"``, never applied
   silently;
3. no match — fails with the closest candidate line numbers and their text.
"""

from __future__ import annotations

from ..errors import NotImplementedYetError
from ..models import MutationResult
from ..workspace import WorkspaceHandle

_NOT_IMPLEMENTED = (
    "text editing is not part of this release; this function is a published stub "
    "and nothing was modified"
)


def replace_text(
    handle: WorkspaceHandle,
    path: str,
    old: str,
    new: str,
    *,
    match: str = "first",
    expected_hash: str | None = None,
    expected_match_count: int | None = None,
    allow_sensitive_write: bool = False,
    dry_run: bool = False,
) -> MutationResult:
    """Replace literal text, reporting which matching stage matched."""

    raise NotImplementedYetError(_NOT_IMPLEMENTED)


def replace_lines(
    handle: WorkspaceHandle,
    path: str,
    start_line: int,
    end_line: int,
    replacement: str,
    *,
    expected_content: str | None = None,
    expected_hash: str | None = None,
    dry_run: bool = False,
) -> MutationResult:
    """Replace an inclusive 1-based line range with ``replacement``."""

    raise NotImplementedYetError(_NOT_IMPLEMENTED)


def insert_lines(
    handle: WorkspaceHandle,
    path: str,
    line: int,
    content: str,
    *,
    position: str = "after",
    expected_hash: str | None = None,
    dry_run: bool = False,
) -> MutationResult:
    """Insert content before or after a 1-based line, or at the end of the file."""

    raise NotImplementedYetError(_NOT_IMPLEMENTED)


def delete_lines(
    handle: WorkspaceHandle,
    path: str,
    start_line: int,
    end_line: int,
    *,
    expected_hash: str | None = None,
    dry_run: bool = False,
) -> MutationResult:
    """Delete an inclusive 1-based line range."""

    raise NotImplementedYetError(_NOT_IMPLEMENTED)


def apply_patch(
    handle: WorkspaceHandle,
    patch_text: str,
    *,
    dry_run: bool = False,
    allow_sensitive_write: bool = False,
) -> MutationResult:
    """Apply a unified diff. Multi-file application is recoverable, not atomic."""

    raise NotImplementedYetError(_NOT_IMPLEMENTED)
