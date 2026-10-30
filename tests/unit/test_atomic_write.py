"""The atomic write primitive: content, permissions, and failure behaviour."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from filekeeper.core.errors import StorageError
from filekeeper.core.safety.atomic import atomic_write_bytes
from filekeeper.core.safety.hashing import hash_bytes


def test_new_file_gets_exact_content_and_hash(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    digest = atomic_write_bytes(target, b"hello\n")

    assert target.read_bytes() == b"hello\n"
    assert digest == hash_bytes(b"hello\n")


def test_existing_permissions_are_preserved(tmp_path: Path) -> None:
    target = tmp_path / "script.sh"
    target.write_bytes(b"old\n")
    os.chmod(target, 0o750)

    atomic_write_bytes(target, b"new\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    assert target.read_bytes() == b"new\n"


def test_explicit_mode_is_applied(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    atomic_write_bytes(target, b"data\n", mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_failure_before_rename_keeps_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "important.txt"
    target.write_bytes(b"original content\n")

    def fail_replace(source: str, destination: str) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr("filekeeper.core.safety.atomic.os.replace", fail_replace)

    with pytest.raises(StorageError):
        atomic_write_bytes(target, b"replacement content\n")

    assert target.read_bytes() == b"original content\n"
    leftovers = [path.name for path in tmp_path.iterdir() if path.name != "important.txt"]
    assert leftovers == []


def test_failure_before_rename_on_new_file_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "brand-new.txt"
    monkeypatch.setattr(
        "filekeeper.core.safety.atomic.os.replace",
        lambda source, destination: (_ for _ in ()).throw(OSError("simulated")),
    )

    with pytest.raises(StorageError):
        atomic_write_bytes(target, b"never landed\n")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_symlink_destination_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_bytes(b"real\n")
    link = tmp_path / "link.txt"
    os.symlink(real, link)

    with pytest.raises(StorageError):
        atomic_write_bytes(link, b"other\n")

    assert real.read_bytes() == b"real\n"


def test_verification_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "verify.txt"
    monkeypatch.setattr(
        "filekeeper.core.safety.atomic.hash_file",
        lambda path: "sha256:" + "0" * 64,
    )

    with pytest.raises(StorageError):
        atomic_write_bytes(target, b"payload\n")

    # The content did land; the error says verification failed.
    assert target.read_bytes() == b"payload\n"
