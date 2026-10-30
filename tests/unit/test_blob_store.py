"""Content-addressed blob storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from filekeeper.core.errors import StorageError
from filekeeper.core.history.blobs import BlobStore
from filekeeper.core.safety.hashing import hash_bytes


def test_put_then_get_roundtrip(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs")
    payload = b"before-image\n"
    digest = store.put_bytes(payload)

    assert digest == hash_bytes(payload)
    assert store.has(digest) is True
    assert store.get_bytes(digest) == payload


def test_identical_content_is_stored_once(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs")
    first = store.put_bytes(b"same\n")
    second = store.put_bytes(b"same\n")

    assert first == second
    files = [path for path in (tmp_path / "blobs").rglob("*") if path.is_file()]
    assert len(files) == 1


def test_blob_path_is_sharded_by_hash_prefix(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs")
    digest = store.put_bytes(b"shard me\n")
    path = store.path_for(digest)
    hex_digest = digest.removeprefix("sha256:")

    assert path.parent.name == hex_digest[:2]
    assert path.name == hex_digest[2:]


def test_missing_blob_is_reported(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs")
    digest = hash_bytes(b"absent\n")
    with pytest.raises(StorageError):
        store.get_bytes(digest)
    assert store.verify(digest) is False


def test_corrupt_blob_is_detected(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs")
    digest = store.put_bytes(b"original\n")
    store.path_for(digest).write_bytes(b"tampered\n")

    assert store.verify(digest) is False
    with pytest.raises(StorageError):
        store.get_bytes(digest)
