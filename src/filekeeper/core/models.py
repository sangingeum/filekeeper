"""Value types returned by Core.

All result types are frozen dataclasses. Paths in results are
workspace-relative POSIX strings so that CLI and MCP output are identical and
free of host-specific absolute paths.

Hashes are SHA-256 and are formatted as ``sha256:<64 hex characters>``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

EntryKind = Literal["file", "directory", "symlink", "other"]


@dataclass(frozen=True, slots=True)
class Workspace:
    """A registered workspace root."""

    id: str
    name: str
    root_path: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """An error rendered as data, for multi-result calls that must not raise."""

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True, slots=True)
class FileReadResult:
    """A bounded read. ``content`` holds the requested lines with their original endings.

    Line numbers are 1-based and inclusive. ``total_lines`` is ``None`` when the
    read was truncated byte-wise before the end of the file; ``next_start_line``
    then names the first line that was not returned.
    """

    path: str
    content: str
    start_line: int
    end_line: int
    returned_line_count: int
    total_lines: int | None
    size_bytes: int
    content_hash: str
    encoding: str
    newline: str
    has_bom: bool
    truncated: bool = False
    truncation_reason: str | None = None
    next_start_line: int | None = None


@dataclass(frozen=True, slots=True)
class ReadManyEntry:
    """One file's outcome inside :meth:`read_multiple_files`; failures are data, not raises."""

    path: str
    ok: bool
    result: FileReadResult | None = None
    error: ErrorDetail | None = None


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    path: str
    name: str
    kind: EntryKind
    size_bytes: int
    is_symlink: bool


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    path: str
    entries: tuple[DirectoryEntry, ...]
    truncated: bool
    max_entries: int


@dataclass(frozen=True, slots=True)
class PathInfo:
    path: str
    kind: EntryKind
    size_bytes: int
    mode: int
    modified_at: str
    is_symlink: bool
    symlink_target: str | None = None
    is_text: bool | None = None
    content_hash: str | None = None
    line_count: int | None = None
    encoding: str | None = None
    newline: str | None = None


@dataclass(frozen=True, slots=True)
class ExistsResult:
    path: str
    exists: bool
    kind: EntryKind | None = None


@dataclass(frozen=True, slots=True)
class PathSearchResult:
    path: str
    glob: str
    paths: tuple[str, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ContentMatch:
    path: str
    line_number: int
    line: str
    column: int


@dataclass(frozen=True, slots=True)
class ContentSearchResult:
    path: str
    query: str
    regex: bool
    matches: tuple[ContentMatch, ...]
    truncated: bool
    files_searched: int


@dataclass(frozen=True, slots=True)
class OperationItemResult:
    """One path touched by a mutation.

    ``operation_item_id`` is reserved for the history layer; it is ``None`` in
    releases that do not record history.

    ``after_hash`` and ``size_bytes`` describe observed state: on a dry run they
    are ``None`` because nothing was written. A dry run puts the predicted digest
    in ``predicted_after_hash`` instead.
    """

    path: str
    change: Literal["created", "modified", "deleted", "moved", "unchanged"]
    before_hash: str | None = None
    after_hash: str | None = None
    size_bytes: int | None = None
    predicted_after_hash: str | None = None


@dataclass(frozen=True, slots=True)
class OperationItemRecord:
    """One path recorded against an operation in the durable history."""

    relative_path: str
    item_type: str
    before_blob_hash: str | None
    after_blob_hash: str | None
    before_exists: bool
    after_exists: bool
    before_mode: int | None
    after_mode: int | None


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """An operation as stored in the history ledger.

    ``operation_id`` is the single key used by every surface: mutations,
    ``history list`` / ``history show``, diff, undo and the MCP tools.
    """

    operation_id: str
    workspace_id: str
    operation_type: str
    target_path: str | None
    status: str
    created_at: str
    actor: str | None
    task_id: str | None
    summary: str | None
    before_hash: str | None
    after_hash: str | None
    items: tuple[OperationItemRecord, ...] = ()
    history_skipped: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiffEntry:
    """One path's before/after comparison inside :func:`diff_operation`."""

    path: str
    item_type: str
    before_exists: bool
    after_exists: bool
    is_binary: bool
    unified_diff: str


@dataclass(frozen=True, slots=True)
class OperationDiff:
    operation_id: str
    items: tuple[DiffEntry, ...]


@dataclass(frozen=True, slots=True)
class PatchHunkResult:
    """One unified-diff hunk and what happened to it.

    ``status`` is ``"applied"`` for a real application, ``"would_apply"`` for a
    dry run, and ``"rejected"`` when a hunk could not be placed. Line numbers in
    a hunk header are 1-based; an empty old side uses position 0.
    """

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    status: str


@dataclass(frozen=True, slots=True)
class PatchFileResult:
    """One path affected by :func:`apply_patch`, with its per-hunk outcomes.

    ``applied`` is ``False`` for a dry run, and ``after_hash`` is then ``None``;
    the predicted digest is carried by ``predicted_after_hash``.
    """

    path: str
    change: Literal["created", "modified", "deleted", "unchanged"]
    applied: bool
    before_hash: str | None
    after_hash: str | None
    hunks: tuple[PatchHunkResult, ...] = ()
    predicted_after_hash: str | None = None


@dataclass(frozen=True, slots=True)
class MutationResult:
    """The stable shape of every mutating call.

    ``operation_id`` names the durable history record for the mutation and is
    ``None`` only for a dry run. ``undo_available`` says whether that record can
    restore the previous bytes: it is ``False`` when an affected file exceeded
    the history size cap, because its previous bytes were deliberately not
    stored.

    ``match_stage`` and ``match_count`` describe text replacement, and
    ``preview_diff`` carries a dry-run unified diff. ``patch_files`` describes a
    patch application; a recovered failure reports what was rolled back in the
    error details.

    A dry run reports what was *observed* and what *would* happen, never what
    happened: ``changed`` is ``False``, ``after_hash`` is ``None``, and no
    operation is recorded. ``would_change`` says whether a real run would alter
    anything, and ``predicted_after_hash`` carries the digest the content would
    have -- a prediction, never an observation.
    """

    workspace: str
    path: str
    changed: bool
    before_hash: str | None
    after_hash: str | None
    summary: str
    operation_id: str | None = None
    undo_available: bool = False
    dry_run: bool = False
    would_change: bool | None = None
    predicted_after_hash: str | None = None
    warnings: tuple[str, ...] = ()
    items: tuple[OperationItemResult, ...] = ()
    affected_count: int | None = None
    history_skipped: tuple[str, ...] = ()
    match_stage: str | None = None
    match_count: int | None = None
    preview_diff: str | None = None
    patch_files: tuple[PatchFileResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = _jsonable(asdict(self))
        return payload


def as_dry_run(
    result: MutationResult,
    *,
    would_change: bool,
    predicted_after_hash: str | None = None,
) -> MutationResult:
    """Return the honest shape of a dry run for ``result``.

    This is the single place the dry-run contract is applied: nothing was
    written, so the result reports ``changed=False``, no observed ``after_hash``
    or item size, no operation id and nothing undoable. The prediction a caller
    may legitimately want survives as ``predicted_after_hash`` on the result,
    each item and each patch file.

    Item ``change`` values agree with ``would_change``: a preview that would
    alter nothing reports every item as ``"unchanged"``, so the item list never
    contradicts the operation-level verdict. The item stays in the list, because
    callers use it to map a preview to the paths it inspected.
    """

    items: list[OperationItemResult] = []
    for item in result.items:
        change: Literal["created", "modified", "deleted", "moved", "unchanged"] = (
            item.change if would_change else "unchanged"
        )
        items.append(
            replace(
                item,
                change=change,
                after_hash=None,
                size_bytes=None,
                predicted_after_hash=(
                    item.predicted_after_hash
                    if item.predicted_after_hash is not None
                    else item.after_hash
                ),
            )
        )

    return replace(
        result,
        changed=False,
        after_hash=None,
        operation_id=None,
        undo_available=False,
        would_change=would_change,
        predicted_after_hash=predicted_after_hash,
        items=tuple(items),
        patch_files=tuple(
            replace(
                patch_file,
                applied=False,
                after_hash=None,
                predicted_after_hash=(
                    patch_file.predicted_after_hash
                    if patch_file.predicted_after_hash is not None
                    else patch_file.after_hash
                ),
            )
            for patch_file in result.patch_files
        ),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _jsonable(item)
            for key, item in value.items()
            if not (item is None and key in {"would_change", "predicted_after_hash"})
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
