"""Read-side history surfaces: one operation, a filtered list, a unified diff."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..diffing import unified_diff_text
from ..errors import HistoryUnavailableError, OperationNotFoundError, StorageError
from ..models import DiffEntry, OperationDiff, OperationItemRecord, OperationRecord
from ..paths import reported_path
from .journal import OperationJournal, _row_to_operation


def _reported_record(root: Path, record: OperationRecord) -> OperationRecord:
    """Render a stored record's paths for a caller, without migrating the row.

    History stores workspace-relative paths; a workspace rooted at ``/`` reports
    them with their leading separator so the string is the absolute path. Every
    other root renders byte-identically to what was stored.
    """

    return replace(
        record,
        target_path=(
            None if record.target_path is None else reported_path(root, record.target_path)
        ),
        items=tuple(
            replace(item, relative_path=reported_path(root, item.relative_path))
            for item in record.items
        ),
        history_skipped=tuple(reported_path(root, path) for path in record.history_skipped),
    )


def get_operation(journal: OperationJournal, operation_id: str) -> OperationRecord:
    record = journal.get(operation_id)
    if record is None:
        raise OperationNotFoundError(
            "no operation with that id in this workspace", details={"operation_id": operation_id}
        )
    return _reported_record(journal.root, record)


def _matching_operations(journal: OperationJournal, path: str | None) -> list[str]:
    connection = journal._connect()
    try:
        if path is None:
            rows = connection.execute("SELECT id FROM operations").fetchall()
        else:
            rows = connection.execute(
                "SELECT DISTINCT o.id FROM operations o "
                "LEFT JOIN operation_items i ON i.operation_id = o.id "
                "WHERE o.target_path = ? OR i.relative_path = ?",
                (path, path),
            ).fetchall()
    except sqlite3.Error as exc:
        raise StorageError(f"cannot read operation history: {exc}") from exc
    finally:
        connection.close()
    return [str(row["id"]) for row in rows]


def list_history(
    journal: OperationJournal,
    *,
    path: str | None = None,
    actor: str | None = None,
    task_id: str | None = None,
    limit: int = 50,
) -> tuple[OperationRecord, ...]:
    """Newest first, filtered by path, actor and task id, in this workspace only."""

    if limit <= 0:
        return ()
    clauses: list[str] = []
    parameters: list[Any] = []
    if actor is not None:
        clauses.append("actor = ?")
        parameters.append(actor)
    if task_id is not None:
        clauses.append("task_id = ?")
        parameters.append(task_id)
    identifiers = _matching_operations(journal, path) if path is not None else None
    if identifiers is not None:
        if not identifiers:
            return ()
        placeholders = ",".join("?" for _ in identifiers)
        clauses.append(f"id IN ({placeholders})")
        parameters.extend(identifiers)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = journal._connect()
    try:
        rows = connection.execute(
            f"SELECT * FROM operations {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        records: list[OperationRecord] = []
        for row in rows:
            items = connection.execute(
                "SELECT * FROM operation_items WHERE operation_id = ? ORDER BY id",
                (row["id"],),
            ).fetchall()
            records.append(_reported_record(journal.root, _row_to_operation(row, items)))
    except sqlite3.Error as exc:
        raise StorageError(f"cannot read operation history: {exc}") from exc
    finally:
        connection.close()
    return tuple(records)


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_blob(journal: OperationJournal, digest: str, path: str) -> bytes:
    try:
        return journal.blobs.get_bytes(digest)
    except Exception as exc:
        raise HistoryUnavailableError(
            f"cannot read the recorded content for {path}: {exc}",
            details={"path": path, "hash": digest},
        ) from exc


def _diff_entry(journal: OperationJournal, item: OperationItemRecord) -> DiffEntry:
    before = b""
    after = b""
    if item.before_exists and item.before_blob_hash is not None:
        before = _read_blob(journal, item.before_blob_hash, item.relative_path)
    if item.after_exists and item.after_blob_hash is not None:
        after = _read_blob(journal, item.after_blob_hash, item.relative_path)
    before_text = _decode(before)
    after_text = _decode(after)
    if before_text is None or after_text is None:
        return DiffEntry(
            path=reported_path(journal.root, item.relative_path),
            item_type=item.item_type,
            before_exists=item.before_exists,
            after_exists=item.after_exists,
            is_binary=True,
            unified_diff="",
        )
    diff = unified_diff_text(
        before_text,
        after_text,
        fromfile=f"a/{item.relative_path}" if item.before_exists else "/dev/null",
        tofile=f"b/{item.relative_path}" if item.after_exists else "/dev/null",
    )
    return DiffEntry(
        path=reported_path(journal.root, item.relative_path),
        item_type=item.item_type,
        before_exists=item.before_exists,
        after_exists=item.after_exists,
        is_binary=False,
        unified_diff=diff,
    )


def diff_operation(journal: OperationJournal, operation_id: str) -> OperationDiff:
    record = journal.get(operation_id)
    if record is None:
        raise OperationNotFoundError(
            "no operation with that id in this workspace", details={"operation_id": operation_id}
        )
    return OperationDiff(
        operation_id=record.operation_id,
        items=tuple(
            _diff_entry(journal, item) for item in record.items if item.item_type == "file"
        ),
    )


__all__ = ["diff_operation", "get_operation", "list_history"]
