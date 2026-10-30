"""SHA-256 hashing helpers.

Hashes are always returned as ``sha256:<hex>``. Incoming hashes are accepted with
or without the prefix, and a malformed value is a validation error rather than a
silent mismatch.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from ..errors import PathNotFoundError, ValidationError, storage_failure

ALGORITHM = "sha256"
PREFIX = f"{ALGORITHM}:"
CHUNK_SIZE = 1024 * 1024
_HEX_LENGTH = 64


def format_digest(digest_hex: str) -> str:
    """Return the canonical ``sha256:<hex>`` form for a bare hex digest."""

    return f"{PREFIX}{digest_hex}"


def hash_bytes(data: bytes) -> str:
    return format_digest(hashlib.sha256(data).hexdigest())


def hash_file(path: Path, *, display_path: str | None = None) -> str:
    """Stream a file through SHA-256; never loads the whole file into memory.

    ``display_path`` is the caller-facing path used in error details. Callers that
    hold a resolved path should pass the workspace-relative form, so a failure
    never reports the host path.
    """

    display = str(path) if display_path is None else display_path
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise PathNotFoundError("file not found", details={"path": display}) from exc
    except OSError as exc:
        raise storage_failure("hash", display, exc) from exc
    return format_digest(digest.hexdigest())


def normalize_hash(value: str) -> str:
    """Validate and canonicalise a caller-supplied hash."""

    candidate = value.strip()
    if candidate.startswith(PREFIX):
        candidate = candidate[len(PREFIX) :]
    valid_characters = set("0123456789abcdefABCDEF")
    if len(candidate) != _HEX_LENGTH or any(char not in valid_characters for char in candidate):
        raise ValidationError(
            "not a SHA-256 hash; expected 64 hexadecimal characters, optionally "
            "prefixed with 'sha256:'",
            details={"value": value},
        )
    return format_digest(candidate.lower())


def hashes_match(left: str, right: str) -> bool:
    """Constant-time comparison of two canonical hashes."""

    return hmac.compare_digest(normalize_hash(left), normalize_hash(right))
