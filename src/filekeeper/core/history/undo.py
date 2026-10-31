"""Safe undo with up-front conflict detection.

Undo never blindly overwrites: every affected path must still match one of the
two states recorded for it — the post-operation state (untouched since the
change) or the pre-operation state (already back where undo wants it, which is
what a partially applied multi-file operation leaves behind). A path that
matches neither was changed by someone else, and the whole undo is refused
before a single byte is written.

The undo is itself recorded as an operation, so the ledger stays truthful.
Redo is deliberately absent from this release; see ``README.md``.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ..errors import (
    FilekeeperError,
    HistoryUnavailableError,
    StorageError,
    UndoConflictError,
    storage_failure,
)
from ..models import MutationResult, OperationItemRecord, OperationItemResult, OperationRecord
from ..safety.atomic import atomic_write_bytes, fsync_directory
from ..safety.hashing import hash_file
from .journal import (
    COMMITTED,
    FAILED,
    UNDO_REFUSED,
    UNDONE,
    OperationJournal,
    PendingItem,
)


@dataclass(frozen=True, slots=True)
class _Step:
    item: OperationItemRecord
    action: str  # "restore-file", "restore-directory", "remove-file", "remove-directory", "none"
    data: bytes | None = None


def _target_path(journal: OperationJournal, relative_path: str) -> Path:
    candidate = (journal.root / relative_path).resolve()
    root = journal.root.resolve()
    if candidate != root and root not in candidate.parents:
        raise StorageError(
            "refusing to undo a path outside the workspace",
            details={"path": relative_path},
        )
    return candidate


def _current(item: OperationItemRecord, path: Path) -> tuple[bool, str | None]:
    if item.item_type == "directory":
        return path.is_dir(), None
    if not path.is_file():
        return False, None
    try:
        return True, hash_file(path, display_path=item.relative_path)
    except OSError:
        return False, None


def _matches(
    exists: bool, digest: str | None, current_exists: bool, current_hash: str | None
) -> bool:
    if exists != current_exists:
        return False
    if not exists:
        return True
    if digest is None:
        return True
    return current_hash == digest


def _load_before(journal: OperationJournal, item: OperationItemRecord) -> bytes:
    if item.before_blob_hash is None:
        raise HistoryUnavailableError(
            f"history was not stored for {item.relative_path}, so it cannot be restored",
            details={"path": item.relative_path, "operation_item": item.relative_path},
        )
    try:
        return journal.blobs.get_bytes(item.before_blob_hash)
    except StorageError as exc:
        raise HistoryUnavailableError(
            f"the recorded content for {item.relative_path} is missing or corrupt: {exc.message}",
            details={"path": item.relative_path, "hash": item.before_blob_hash},
        ) from exc


def _plan(journal: OperationJournal, record: OperationRecord) -> list[_Step]:
    steps: list[_Step] = []
    conflicts: list[str] = []
    already_before = 0

    for item in record.items:
        path = _target_path(journal, item.relative_path)
        current_exists, current_hash = _current(item, path)
        matches_before = _matches(
            item.before_exists, item.before_blob_hash, current_exists, current_hash
        )
        matches_after = _matches(
            item.after_exists, item.after_blob_hash, current_exists, current_hash
        )
        if not matches_before and not matches_after:
            conflicts.append(item.relative_path)
            continue
        if matches_before:
            # A committed operation left every item in its post-operation state.
            # Matching only the pre-operation state now means the path was
            # changed or removed by someone else, not that this undo already
            # ran. Only a failed operation can legitimately be part-way back.
            if record.status == COMMITTED and not matches_after:
                conflicts.append(item.relative_path)
                continue
            already_before += 1
            continue

        if not item.before_exists:
            action = "remove-directory" if item.item_type == "directory" else "remove-file"
            steps.append(_Step(item=item, action=action))
            continue

        if item.item_type == "directory":
            steps.append(_Step(item=item, action="restore-directory"))
        else:
            steps.append(_Step(item=item, action="restore-file", data=_load_before(journal, item)))

    # A directory may only be removed when every entry inside it is also being
    # removed by this same undo. That keeps "undo of a directory create" from
    # destroying files the user added afterwards, while allowing a directory
    # move (whose children are all recorded) to be reversed.
    removal_paths = {
        journal.root.joinpath(step.item.relative_path).resolve()
        for step in steps
        if step.action in {"remove-file", "remove-directory"}
    }
    for step in steps:
        if step.action != "remove-directory":
            continue
        directory = _target_path(journal, step.item.relative_path)
        try:
            leftovers = [
                entry for entry in directory.iterdir() if entry.resolve() not in removal_paths
            ]
        except OSError:
            leftovers = [directory]
        if leftovers:
            conflicts.append(step.item.relative_path)

    if conflicts:
        named = ", ".join(sorted(conflicts))
        raise UndoConflictError(
            f"cannot undo: these paths changed after the operation ({named}), "
            "so nothing was modified",
            details={
                "operation_id": record.operation_id,
                "conflicting_paths": named,
            },
        )
    if record.status == FAILED and already_before == len(record.items):
        raise UndoConflictError(
            "operation did not take effect; there is nothing to undo",
            details={"operation_id": record.operation_id},
        )
    if record.status == UNDONE:
        raise UndoConflictError(
            "operation is already undone", details={"operation_id": record.operation_id}
        )
    if record.status not in {COMMITTED, FAILED}:
        raise UndoConflictError(
            "operation is not committed and cannot be undone",
            details={"operation_id": record.operation_id, "status": record.status},
        )
    if not steps:
        # Every item already holds its pre-operation state, which for a committed
        # operation means the write left the content unchanged. There is nothing
        # to restore, and reporting success would claim a change that never
        # happened.
        raise UndoConflictError(
            "cannot undo: the operation left its target in the pre-operation state, "
            "so undoing it would change nothing",
            details={"operation_id": record.operation_id},
        )
    return steps


def _apply(journal: OperationJournal, steps: list[_Step]) -> None:
    directories = [step for step in steps if step.action == "restore-directory"]
    for step in sorted(directories, key=lambda entry: entry.item.relative_path.count("/")):
        path = _target_path(journal, step.item.relative_path)
        try:
            path.mkdir(parents=True, exist_ok=True)
            if step.item.before_mode is not None:
                os.chmod(path, stat.S_IMODE(step.item.before_mode))
        except OSError as exc:
            raise storage_failure("restore directory", step.item.relative_path, exc) from exc

    for step in steps:
        if step.action != "restore-file":
            continue
        path = _target_path(journal, step.item.relative_path)
        try:
            atomic_write_bytes(
                path,
                step.data or b"",
                mode=step.item.before_mode,
                fsync=journal.config.fsync,
                display_path=step.item.relative_path,
            )
        except OSError as exc:
            raise storage_failure("restore file", step.item.relative_path, exc) from exc

    removable = [step for step in steps if step.action in {"remove-file", "remove-directory"}]
    files = [step for step in removable if step.action == "remove-file"]
    for step in files:
        path = _target_path(journal, step.item.relative_path)
        try:
            path.unlink()
            fsync_directory(path.parent)
        except OSError as exc:
            raise storage_failure(
                "remove file during undo", step.item.relative_path, exc
            ) from exc

    directories = [step for step in removable if step.action == "remove-directory"]
    for step in sorted(
        directories, key=lambda entry: entry.item.relative_path.count("/"), reverse=True
    ):
        path = _target_path(journal, step.item.relative_path)
        try:
            path.rmdir()
            fsync_directory(path.parent)
        except OSError as exc:
            raise storage_failure(
                "remove directory during undo", step.item.relative_path, exc
            ) from exc


def _undo_items(record: OperationRecord) -> list[PendingItem]:
    items: list[PendingItem] = []
    for item in record.items:
        items.append(
            PendingItem(
                relative_path=item.relative_path,
                item_type=item.item_type,
                before_exists=item.after_exists,
                after_exists=item.before_exists,
                before_hash=item.after_blob_hash,
                after_hash=item.before_blob_hash,
                before_mode=item.after_mode,
                after_mode=item.before_mode,
            )
        )
    return items


def _result(
    journal: OperationJournal,
    record: OperationRecord,
    undo_operation_id: str,
    *,
    workspace_name: str,
    steps: list[_Step],
) -> MutationResult:
    items = tuple(
        OperationItemResult(
            path=step.item.relative_path,
            change=(
                "deleted"
                if not step.item.before_exists
                else "created"
                if not step.item.after_exists
                else "modified"
            ),
            before_hash=step.item.after_blob_hash,
            after_hash=step.item.before_blob_hash,
        )
        for step in steps
        if step.action != "none"
    )
    return MutationResult(
        workspace=workspace_name,
        path=record.target_path or "",
        changed=True,
        before_hash=record.after_hash,
        after_hash=record.before_hash,
        summary=f"Undid operation {record.operation_id}.",
        operation_id=undo_operation_id,
        undo_available=True,
        items=items,
        affected_count=len(items),
    )


def _conflicting_paths(error: UndoConflictError) -> list[str]:
    """The refusal's paths as a list, still caller-relative as Core recorded them."""

    named = error.details.get("conflicting_paths")
    if not isinstance(named, str):
        return []
    return sorted({part.strip() for part in named.split(",") if part.strip()})


def _record_refusal(
    journal: OperationJournal,
    record: OperationRecord,
    error: UndoConflictError,
    *,
    actor: str | None,
    task_id: str | None,
) -> str | None:
    """Record a refused undo attempt so the ledger shows it was tried.

    A refusal changes nothing, so the row carries no items and no before/after
    hashes. Its status is ``failed`` — the attempt did not take effect — and its
    type keeps it out of the undo stack (:func:`latest_undoable`). The conflict
    code and the paths that blocked the undo travel in metadata. Recording is
    best-effort: a storage failure here must never replace the refusal the
    caller is owed.
    """

    try:
        begun = journal.begin(
            operation_type=UNDO_REFUSED,
            target_path=record.target_path,
            summary=f"Refused to undo operation {record.operation_id}: {error.message}",
            items=[],
            actor=actor,
            task_id=task_id,
            metadata={
                "undone_operation": record.operation_id,
                "conflict_code": str(error.code),
                "conflicting_paths": _conflicting_paths(error),
            },
        )
        journal.abort(begun.operation_id)
    except FilekeeperError:
        return None
    return begun.operation_id


def undo_operation(
    journal: OperationJournal,
    operation_id: str,
    *,
    workspace_name: str,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Restore the recorded pre-state of one operation, or refuse and change nothing."""

    record = journal.get(operation_id)
    if record is None:
        from ..errors import OperationNotFoundError

        raise OperationNotFoundError(
            "no operation with that id in this workspace", details={"operation_id": operation_id}
        )
    try:
        steps = _plan(journal, record)
    except UndoConflictError as exc:
        # A refusal of another refusal adds nothing: the first row already
        # records that this conflict was seen and nothing changed. Journalling
        # the repeat would be noise, not new evidence, so only a refusal of a
        # real operation is recorded.
        if record.operation_type != UNDO_REFUSED:
            _record_refusal(journal, record, exc, actor=actor, task_id=task_id)
        raise
    undo_items = _undo_items(record)

    begun = journal.begin(
        operation_type="undo",
        target_path=record.target_path,
        summary=f"Undid operation {record.operation_id}.",
        items=undo_items,
        actor=actor,
        task_id=task_id,
        before_hash=record.after_hash,
        after_hash=record.before_hash,
        metadata={"undone_operation": record.operation_id},
    )
    try:
        _apply(journal, steps)
    except Exception:
        journal.abort(begun.operation_id)
        raise
    journal.commit(begun.operation_id)
    journal._set_status(record.operation_id, UNDONE)
    return _result(
        journal, record, begun.operation_id, workspace_name=workspace_name, steps=steps
    )


def latest_undoable(journal: OperationJournal, *, actor: str | None = None) -> OperationRecord:
    """The most recent operation this workspace may undo.

    A recorded refusal changed nothing; it is not a mutation and is never
    offered as the next undo target, so ``undo-last`` skips those rows.
    """

    clauses = ["status IN (?, ?)", "operation_type != ?"]
    parameters: list[object] = [COMMITTED, FAILED, UNDO_REFUSED]
    if actor is not None:
        clauses.append("actor = ?")
        parameters.append(actor)
    connection = journal._connect()
    try:
        row = connection.execute(
            f"SELECT * FROM operations WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            tuple(parameters),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise UndoConflictError(
            "no undoable operation in this workspace",
            details={"actor": actor},
        )
    record = journal.get(str(row["id"]))
    assert record is not None
    return record


def undo_last(
    journal: OperationJournal,
    *,
    workspace_name: str,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Undo the most recent operation in *this* workspace; never another one's."""

    record = latest_undoable(journal, actor=actor)
    return undo_operation(
        journal, record.operation_id, workspace_name=workspace_name, actor=actor, task_id=task_id
    )


__all__ = ["latest_undoable", "undo_last", "undo_operation"]
