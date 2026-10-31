"""The durable operation journal.

A mutation is recorded in this order, and the order matters:

1. content-addressed blobs are written and fsynced (immutable, so writing them
   first is always safe and never needs rollback);
2. the operation row is inserted with status ``pending`` on a connection in
   SQLite full-synchronous mode, so a lost flush cannot erase the only record of
   what is about to happen;
3. the caller replaces the target (per-file atomic);
4. the caller verifies the resulting hash;
5. the operation is marked ``committed``.

If the process dies anywhere after step 2, :meth:`OperationJournal.reconcile`
resolves the pending row on the next opening by comparing each affected path's
current state with the recorded before and after states. It never silently
discards the record, and it never deletes blobs: the previous bytes stay
recoverable.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import database
from ..config import Config
from ..errors import StorageError
from ..models import OperationItemRecord, OperationRecord
from ..safety.hashing import hash_bytes, hash_file
from .blobs import BlobStore

PENDING = "pending"
COMMITTED = "committed"
UNDONE = "undone"
FAILED = "failed"
PARTIALLY_UNDONE = "partially_undone"
# A refused undo is recorded so the ledger shows the attempt, but it is not a
# mutation and is never offered as undoable.
UNDO_REFUSED = "undo_refused"


@dataclass(frozen=True, slots=True)
class PendingItem:
    """One path's before and after state, as the caller knows it.

    ``before_bytes`` / ``after_bytes`` are staged into the blob store when they
    are inside the size cap. A hash may be supplied without bytes when the
    caller chose not to read the content (an oversize file): the row then keeps
    the hash but names no blob, and undo reports that history was skipped.
    """

    relative_path: str
    item_type: str
    before_exists: bool
    after_exists: bool
    before_bytes: bytes | None = None
    after_bytes: bytes | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    before_mode: int | None = None
    after_mode: int | None = None


@dataclass(frozen=True, slots=True)
class BeginResult:
    operation_id: str
    skipped: tuple[str, ...]
    warnings: tuple[str, ...]


class OperationJournal:
    """Records operations, stages blobs, and resolves interrupted operations."""

    def __init__(
        self,
        database_path: Path,
        *,
        root: Path,
        workspace_id: str,
        config: Config,
        blobs: BlobStore | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.root = Path(root)
        self.workspace_id = workspace_id
        self.config = config
        self.blobs = blobs or BlobStore(self.database_path.parent / "blobs", fsync=config.fsync)

    # -- connections ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        return database.open_workspace_database(self.database_path)

    # -- recording -----------------------------------------------------------
    def _stage(
        self, data: bytes | None, digest: str | None, skipped: list[str], path: str
    ) -> str | None:
        if data is None:
            # A hash without bytes is only "skipped" when the store genuinely
            # does not hold those bytes. An undo names hashes whose blobs the
            # original operation already stored, so its history is complete.
            if digest is not None and not self.blobs.has(digest):
                skipped.append(path)
            return digest
        content_hash = hash_bytes(data)
        if len(data) > self.config.max_history_blob_bytes:
            skipped.append(path)
            return content_hash
        self.blobs.put_bytes(data)
        return content_hash

    def begin(
        self,
        *,
        operation_type: str,
        target_path: str | None,
        summary: str,
        items: list[PendingItem],
        actor: str | None = None,
        task_id: str | None = None,
        before_hash: str | None = None,
        after_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BeginResult:
        """Stage blobs, then insert the pending operation and its items."""

        skipped: list[str] = []
        staged: list[tuple[PendingItem, str | None, str | None]] = []
        blob_rows: list[tuple[str, int]] = []

        for item in items:
            before = self._stage(item.before_bytes, item.before_hash, skipped, item.relative_path)
            after = self._stage(item.after_bytes, item.after_hash, skipped, item.relative_path)
            staged.append((item, before, after))
            for data, digest in ((item.before_bytes, before), (item.after_bytes, after)):
                if data is None or digest is None:
                    continue
                if len(data) > self.config.max_history_blob_bytes:
                    continue
                blob_rows.append((digest, len(data)))

        operation_id = f"op_{database.uuid_token()}"
        payload: dict[str, Any] = dict(metadata or {})
        if skipped:
            payload["history_skipped"] = sorted(set(skipped))
        now = database.utc_now()

        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO operations (id, workspace_id, operation_type, target_path, "
                    "status, created_at, actor, task_id, summary, before_hash, after_hash, "
                    "metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        self.workspace_id,
                        operation_type,
                        target_path,
                        PENDING,
                        now,
                        actor,
                        task_id,
                        summary,
                        before_hash,
                        after_hash,
                        json.dumps(payload, sort_keys=True),
                    ),
                )
                for item, before, after in staged:
                    connection.execute(
                        "INSERT INTO operation_items (operation_id, relative_path, item_type, "
                        "before_blob_hash, after_blob_hash, before_exists, after_exists, "
                        "before_mode, after_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            operation_id,
                            item.relative_path,
                            item.item_type,
                            before,
                            after,
                            int(item.before_exists),
                            int(item.after_exists),
                            item.before_mode,
                            item.after_mode,
                        ),
                    )
                for digest, size in blob_rows:
                    connection.execute(
                        "INSERT OR IGNORE INTO blobs (hash, size, created_at) VALUES (?, ?, ?)",
                        (digest, size, now),
                    )
        except sqlite3.Error as exc:
            raise StorageError(
                f"cannot record operation: {exc}", details={"operation_type": operation_type}
            ) from exc
        finally:
            connection.close()

        warnings = tuple(
            f"history was skipped for {path}: it exceeds the history size cap "
            f"({self.config.max_history_blob_bytes} bytes), so it cannot be undone"
            for path in sorted(set(skipped))
        )
        return BeginResult(
            operation_id=operation_id,
            skipped=tuple(sorted(set(skipped))),
            warnings=warnings,
        )

    def commit(self, operation_id: str) -> None:
        """Mark an operation committed once its filesystem change is verified."""

        self._set_status(operation_id, COMMITTED)

    def abort(self, operation_id: str) -> None:
        """Mark an operation failed; blobs stay behind so nothing is lost."""

        self._set_status(operation_id, FAILED)

    def _set_status(self, operation_id: str, status: str) -> None:
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "UPDATE operations SET status = ? WHERE id = ?", (status, operation_id)
                )
        except sqlite3.Error as exc:
            raise StorageError(
                f"cannot update operation status: {exc}", details={"operation_id": operation_id}
            ) from exc
        finally:
            connection.close()

    # -- reading -------------------------------------------------------------
    def get(self, operation_id: str) -> OperationRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                return None
            items = connection.execute(
                "SELECT * FROM operation_items WHERE operation_id = ? ORDER BY id",
                (operation_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(
                f"cannot read operation history: {exc}", details={"operation_id": operation_id}
            ) from exc
        finally:
            connection.close()
        return _row_to_operation(row, items)

    def recent(self, *, limit: int = 20) -> tuple[OperationRecord, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM operations ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"cannot read operation history: {exc}") from exc
        finally:
            connection.close()
        return tuple(_row_to_operation(row, ()) for row in rows)

    # -- recovery ------------------------------------------------------------
    def reconcile(self) -> tuple[str, ...]:
        """Resolve operations left pending by an interrupted run.

        Each affected path is compared with the recorded before and after
        states. All-after means the change landed (committed); all-before means
        it did not (failed); anything else is reported as partially applied and
        kept as failed. Blobs are never removed, so recovery stays possible.
        """

        connection = self._connect()
        try:
            pending = connection.execute(
                "SELECT * FROM operations WHERE status = ? ORDER BY created_at", (PENDING,)
            ).fetchall()
            notes: list[str] = []
            for row in pending:
                items = connection.execute(
                    "SELECT * FROM operation_items WHERE operation_id = ? ORDER BY id",
                    (row["id"],),
                ).fetchall()
                status, unresolved = _classify_pending(self.root, items)
                metadata = _load_metadata(row["metadata_json"])
                metadata["reconciled"] = status
                if unresolved:
                    metadata["unresolved"] = unresolved
                with connection:
                    connection.execute(
                        "UPDATE operations SET status = ?, metadata_json = ? WHERE id = ?",
                        (status, json.dumps(metadata, sort_keys=True), row["id"]),
                    )
                detail = ""
                if unresolved:
                    detail = f" ({len(unresolved)} path(s) match neither recorded state)"
                notes.append(f"{row['id']}: pending operation resolved as {status}{detail}")
        except sqlite3.Error as exc:
            raise StorageError(f"cannot reconcile pending operations: {exc}") from exc
        finally:
            connection.close()
        return tuple(notes)


def _load_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _current_state(root: Path, item: sqlite3.Row | OperationItemRecord) -> tuple[bool, str | None]:
    relative = (
        str(item["relative_path"]) if isinstance(item, sqlite3.Row) else item.relative_path
    )
    path = root / relative
    if str(item["item_type"] if isinstance(item, sqlite3.Row) else item.item_type) == "directory":
        return path.is_dir(), None
    if not path.is_file():
        return False, None
    try:
        return True, hash_file(path, display_path=relative)
    except Exception:
        return False, None


def _field(row: sqlite3.Row | OperationItemRecord, name: str) -> Any:
    return row[name] if isinstance(row, sqlite3.Row) else getattr(row, name)


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


def _classify_pending(
    root: Path, items: list[sqlite3.Row]
) -> tuple[str, list[str]]:
    if not items:
        return FAILED, []
    all_after = True
    all_before = True
    unresolved: list[str] = []
    for item in items:
        current_exists, current_hash = _current_state(root, item)
        if not _matches(
            bool(item["after_exists"]), item["after_blob_hash"], current_exists, current_hash
        ):
            all_after = False
        if not _matches(
            bool(item["before_exists"]), item["before_blob_hash"], current_exists, current_hash
        ):
            all_before = False
        after_matches = _matches(
            bool(item["after_exists"]), item["after_blob_hash"], current_exists, current_hash
        )
        before_matches = _matches(
            bool(item["before_exists"]), item["before_blob_hash"], current_exists, current_hash
        )
        if not (after_matches or before_matches):
            unresolved.append(str(item["relative_path"]))
    if all_after:
        return COMMITTED, []
    if all_before:
        return FAILED, []
    return FAILED, unresolved


def _row_to_operation(row: sqlite3.Row, items: Any) -> OperationRecord:
    metadata = _load_metadata(row["metadata_json"])
    skipped = metadata.get("history_skipped", [])
    return OperationRecord(
        operation_id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        operation_type=str(row["operation_type"]),
        target_path=None if row["target_path"] is None else str(row["target_path"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        actor=None if row["actor"] is None else str(row["actor"]),
        task_id=None if row["task_id"] is None else str(row["task_id"]),
        summary=None if row["summary"] is None else str(row["summary"]),
        before_hash=None if row["before_hash"] is None else str(row["before_hash"]),
        after_hash=None if row["after_hash"] is None else str(row["after_hash"]),
        items=tuple(
            OperationItemRecord(
                relative_path=str(item["relative_path"]),
                item_type=str(item["item_type"]),
                before_blob_hash=None
                if item["before_blob_hash"] is None
                else str(item["before_blob_hash"]),
                after_blob_hash=None
                if item["after_blob_hash"] is None
                else str(item["after_blob_hash"]),
                before_exists=bool(item["before_exists"]),
                after_exists=bool(item["after_exists"]),
                before_mode=None if item["before_mode"] is None else int(item["before_mode"]),
                after_mode=None if item["after_mode"] is None else int(item["after_mode"]),
            )
            for item in items
        ),
        history_skipped=tuple(str(path) for path in skipped),
        metadata=metadata,
    )


__all__ = [
    "COMMITTED",
    "FAILED",
    "PARTIALLY_UNDONE",
    "PENDING",
    "UNDONE",
    "UNDO_REFUSED",
    "BeginResult",
    "OperationJournal",
    "PendingItem",
]
