"""Refusal to write credential and key material.

A cheap guard on the most expensive mistake. The rule is a name-shape check on
the workspace-relative target: it refuses obvious credential files and anything
under a key directory. It is not a content scanner, and it is overridable only
through a Core argument and a CLI flag whose names state the risk.
"""

from __future__ import annotations

from typing import Final

SENSITIVE_FILENAMES: Final[frozenset[str]] = frozenset(
    {
        ".env",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "authorized_keys",
        "known_hosts",
        ".netrc",
        ".pgpass",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
    }
)
SENSITIVE_SUFFIXES: Final[tuple[str, ...]] = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
    ".jks",
)
SENSITIVE_DIRECTORIES: Final[frozenset[str]] = frozenset({".ssh", ".gnupg", ".aws"})
_ENV_PREFIX = ".env."


def sensitive_reason(relative_path: str) -> str | None:
    """Return a short reason when the relative path looks like credential material."""

    parts = [part for part in relative_path.replace("\\", "/").split("/") if part]
    if not parts:
        return None

    for part in parts[:-1]:
        if part.lower() in SENSITIVE_DIRECTORIES:
            return f"inside the {part}/ directory"

    name = parts[-1]
    lowered = name.lower()
    if lowered in SENSITIVE_FILENAMES:
        return "credential file name"
    if lowered.startswith(_ENV_PREFIX):
        return "environment file name"
    if lowered.endswith(SENSITIVE_SUFFIXES):
        return "key or certificate file extension"
    return None


def is_sensitive_path(relative_path: str) -> bool:
    return sensitive_reason(relative_path) is not None
