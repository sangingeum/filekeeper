"""Hash formatting, validation and comparison."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from filekeeper.core.errors import ValidationError
from filekeeper.core.safety.hashing import hash_bytes, hash_file, hashes_match, normalize_hash


def test_hash_is_prefixed_sha256() -> None:
    expected = "sha256:" + hashlib.sha256(b"payload").hexdigest()
    assert hash_bytes(b"payload") == expected


def test_file_hash_matches_byte_hash(tmp_path: Path) -> None:
    target = tmp_path / "big.bin"
    payload = b"x" * (3 * 1024 * 1024)
    target.write_bytes(payload)
    assert hash_file(target) == hash_bytes(payload)


def test_normalize_accepts_bare_and_prefixed_hashes() -> None:
    bare = hashlib.sha256(b"a").hexdigest()
    assert normalize_hash(bare) == f"sha256:{bare}"
    assert normalize_hash(f"sha256:{bare.upper()}") == f"sha256:{bare}"


@pytest.mark.parametrize("value", ["", "sha256:short", "md5:" + "a" * 32, "z" * 64])
def test_malformed_hashes_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        normalize_hash(value)


def test_comparison_uses_canonical_form() -> None:
    digest = hashlib.sha256(b"x").hexdigest()
    assert hashes_match(digest, f"sha256:{digest}") is True
    assert hashes_match(digest, hashlib.sha256(b"y").hexdigest()) is False
