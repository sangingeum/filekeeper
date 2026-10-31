"""Credential-shaped targets are refused, and the override is explicit."""

from __future__ import annotations

from pathlib import Path

import pytest

from filekeeper.core.errors import SensitivePathRefusedError


@pytest.mark.parametrize(
    "target",
    [".env", ".env.local", "id_rsa", "cert.pem", "server.key", ".ssh/config"],
)
def test_writing_credential_shaped_paths_is_refused(service, target: str) -> None:
    with pytest.raises(SensitivePathRefusedError):
        service.create_file(target, "placeholder\n")

    assert not (service.root / target).exists()


def test_write_to_an_existing_credential_file_is_refused(service) -> None:
    (service.root / ".env").write_text("TOKEN=keep\n", encoding="utf-8")

    with pytest.raises(SensitivePathRefusedError):
        service.write_file(".env", "TOKEN=change\n")

    assert (service.root / ".env").read_text(encoding="utf-8") == "TOKEN=keep\n"


def test_copy_and_move_to_credential_paths_are_refused(service) -> None:
    with pytest.raises(SensitivePathRefusedError):
        service.copy_file("notes.txt", "backup.pem")
    with pytest.raises(SensitivePathRefusedError):
        service.move_file("notes.txt", ".ssh/authorized_keys")

    assert (service.root / "notes.txt").exists()


def test_override_flag_name_states_the_risk(service) -> None:
    result = service.create_file(".env", "DEBUG=1\n", allow_sensitive_write=True)

    assert result.changed is True
    assert (service.root / ".env").read_text(encoding="utf-8") == "DEBUG=1\n"
    assert any("overridden" in warning for warning in result.warnings)


def test_the_refusal_message_names_the_override(service) -> None:
    """Core's own message names Core's keyword and no adapter's argument."""

    with pytest.raises(SensitivePathRefusedError) as caught:
        service.create_file("id_rsa", "key\n")

    message = caught.value.message
    assert "allow_sensitive_write=True" in message
    assert "--i-accept-secret-write-risk" not in message
    assert "i_accept_secret_write_risk" not in message
    assert caught.value.code == "SENSITIVE_PATH_REFUSED"
    assert set(caught.value.details) == {"path", "reason"}


def test_reads_of_credential_files_are_still_allowed(service) -> None:
    (service.root / ".env").write_text("TOKEN=visible\n", encoding="utf-8")
    assert service.read_file(".env").content == "TOKEN=visible\n"


def test_ordinary_paths_are_unaffected(service) -> None:
    result = service.create_file("environment.md", "notes\n")
    assert result.changed is True


def test_workspace_ignored_warning_is_reported(app, workspace_root: Path) -> None:
    """A git-ignored target still writes, but the result says so."""

    import subprocess

    (workspace_root / ".gitignore").write_text("secret-notes.txt\n", encoding="utf-8")
    completed = subprocess.run(
        ["git", "init", "-q", str(workspace_root)], capture_output=True, check=False
    )
    assert completed.returncode == 0

    app.create_workspace("project", workspace_root)
    service = app.workspace(name="project")
    result = service.create_file("secret-notes.txt", "hidden\n")

    assert result.changed is True
    assert any("ignored by version control" in warning for warning in result.warnings)
