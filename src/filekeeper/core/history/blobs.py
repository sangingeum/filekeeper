"""Content-addressed blob storage.

Immutable, SHA-256 named, written atomically and fsynced. The history layer will
call :meth:`BlobStore.put_bytes` for before-images *before* it records a pending
operation, so a lost journal record can never be the only thing standing between
a user and their previous bytes.

Blobs live at ``<state>/blobs/<first two hex>/<remaining hex>``, so a large store
does not become one directory with millions of entries.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..errors import StorageError, ValidationError
from ..safety.atomic import atomic_write_bytes
from ..safety.hashing import ALGORITHM, format_digest, hash_bytes, normalize_hash


class BlobStore:
    """A directory of content-addressed files."""

    def __init__(self, directory: Path, *, fsync: bool = True) -> None:
        self.directory = Path(directory)
        self.fsync = fsync

    def _relative_parts(self, digest: str) -> tuple[str, str]:
        hex_digest = normalize_hash(digest).removeprefix(f"{ALGORITHM}:")
        return hex_digest[:2], hex_digest[2:]

    def path_for(self, digest: str) -> Path:
        first, rest = self._relative_parts(digest)
        return self.directory / first / rest

    def has(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def put_bytes(self, data: bytes) -> str:
        """Store ``data`` if absent and return its canonical ``sha256:`` hash."""

        digest = hash_bytes(data)
        target = self.path_for(digest)
        if target.is_file() and target.stat().st_size == len(data):
            return digest
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.parent.chmod(stat.S_IRWXU)
            atomic_write_bytes(target, data, mode=0o600, fsync=self.fsync)
            os.chmod(target.parent, stat.S_IRWXU)
        except OSError as exc:  # pragma: no cover - filesystem failure
            raise StorageError(f"cannot write blob: {exc}", details={"hash": digest}) from exc
        return digest

    def get_bytes(self, digest: str) -> bytes:
        """Read a blob and verify its hash before returning it."""

        target = self.path_for(digest)
        try:
            data = target.read_bytes()
        except FileNotFoundError as exc:
            raise StorageError(
                "blob is missing from the store", details={"hash": normalize_hash(digest)}
            ) from exc
        except OSError as exc:  # pragma: no cover - filesystem failure
            raise StorageError(f"cannot read blob: {exc}") from exc
        actual = hash_bytes(data)
        if actual != normalize_hash(digest):
            raise StorageError(
                "blob content does not match its hash; the store is corrupt",
                details={"hash": normalize_hash(digest), "actual_hash": actual},
            )
        return data

    def verify(self, digest: str) -> bool:
        """True when the blob exists and its bytes still hash to its name."""

        try:
            self.get_bytes(digest)
        except (StorageError, ValidationError):
            return False
        return True


__all__ = ["BlobStore", "format_digest"]
