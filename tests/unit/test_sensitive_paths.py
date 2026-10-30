"""Credential-shaped path detection."""

from __future__ import annotations

import pytest

from filekeeper.core.safety.sensitive import is_sensitive_path, sensitive_reason


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.production",
        "config/.env",
        "id_rsa",
        "secrets/id_ed25519",
        "certs/server.pem",
        "certs/server.key",
        "keystore.p12",
        "bundle.pfx",
        ".ssh/config",
        "home/.gnupg/trustdb.gpg",
        "credentials.json",
    ],
)
def test_credential_shaped_paths_are_detected(path: str) -> None:
    assert is_sensitive_path(path) is True
    assert sensitive_reason(path) is not None


@pytest.mark.parametrize(
    "path",
    [
        "notes.txt",
        "src/main.py",
        "environment.md",
        "keys.md",
        "env.example",
        "docs/id_rsa_guide.md",
    ],
)
def test_ordinary_paths_are_not_flagged(path: str) -> None:
    assert is_sensitive_path(path) is False


def test_reason_names_the_rule() -> None:
    assert sensitive_reason(".env") == "environment file name" or (
        sensitive_reason(".env") == "credential file name"
    )
    assert sensitive_reason("certs/key.pem") == "key or certificate file extension"
    assert sensitive_reason(".ssh/config") == "inside the .ssh/ directory"
