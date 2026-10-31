"""The documented setup step must not defeat the ``--root /`` fallback.

The published 0.1.3 defect: ``keeper init`` wrote ``state_location = "project"``
as a live key, which made the built-in default look like an operator decision.
An explicit choice refuses the global fallback, so ``--root /`` failed with a
permission error for anyone who ran the documented setup step first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from filekeeper.cli import exit_codes

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="root can write at the filesystem root"
)


def _relative_to_filesystem_root(tmp_path: Path, name: str) -> str:
    return str((tmp_path / name).relative_to("/"))


def test_create_read_history_undo_at_root_after_init(run_cli, tmp_path: Path) -> None:
    target = _relative_to_filesystem_root(tmp_path, "fk-cli-probe.txt")

    code, out, err = run_cli("init")
    assert code == exit_codes.SUCCESS, err
    assert "initialized" in out

    code, out, err = run_cli(
        "--root", "/", "--json", "file", "create", target, "--content", "hi\n"
    )
    assert code == exit_codes.SUCCESS, (out, err)
    created = json.loads(out)
    assert created["ok"] is True
    assert created["operation_id"]
    warnings = " ".join(created.get("warnings", []))
    assert "global" in warnings

    code, out, _ = run_cli("--root", "/", "--json", "file", "read", target)
    assert code == exit_codes.SUCCESS
    read = json.loads(out)
    assert read["ok"] is True
    assert read["content"] == "hi\n"

    code, out, _ = run_cli("--root", "/", "--json", "history", "list")
    assert code == exit_codes.SUCCESS
    history = json.loads(out)
    operations = history.get("operations", history.get("history", []))
    assert [entry["operation_id"] for entry in operations] == [created["operation_id"]]

    code, out, _ = run_cli(
        "--root", "/", "--json", "history", "undo", created["operation_id"]
    )
    assert code == exit_codes.SUCCESS, out
    assert json.loads(out)["ok"] is True
    assert not (tmp_path / "fk-cli-probe.txt").exists()


def test_dry_run_at_root_after_init_writes_nothing(run_cli, tmp_path: Path) -> None:
    target = _relative_to_filesystem_root(tmp_path, "fk-cli-dry-probe.txt")

    code, out, err = run_cli("init")
    assert code == exit_codes.SUCCESS, err

    code, out, err = run_cli(
        "--root",
        "/",
        "--json",
        "file",
        "create",
        target,
        "--content",
        "hi\n",
        "--dry-run",
    )
    assert code == exit_codes.SUCCESS, (out, err)
    preview = json.loads(out)
    assert preview["ok"] is True
    assert preview["dry_run"] is True
    assert preview["operation_id"] is None
    assert "global" in " ".join(preview.get("warnings", []))
    assert not (tmp_path / "fk-cli-dry-probe.txt").exists()


def test_a_state_directory_error_names_no_host_path(run_cli, home: Path, tmp_path: Path) -> None:
    """A genuine explicit choice fails loudly, and the envelope stays readable."""

    code, _, _ = run_cli("init")
    assert code == exit_codes.SUCCESS

    config_file = home / "config.toml"
    text = config_file.read_text(encoding="utf-8")
    config_file.write_text(
        text.replace('# state_location = "project"', 'state_location = "project"'),
        encoding="utf-8",
    )

    target = _relative_to_filesystem_root(tmp_path, "fk-cli-explicit-probe.txt")
    code, out, _ = run_cli(
        "--root", "/", "--json", "file", "create", target, "--content", "hi\n"
    )

    assert code == exit_codes.STORAGE_ERROR
    payload = json.loads(out)
    assert payload["ok"] is False
    error = payload["error"]
    assert error["code"] == "STORAGE_ERROR"
    assert error["details"]["state_dir"] == ".filekeeper"
    rendered = json.dumps(error["details"])
    assert str(home) not in rendered
    assert "state_dir" in error["details"]


def test_an_argument_shape_mistake_keeps_the_taxonomy(run_cli, tmp_path: Path) -> None:
    code, _, _ = run_cli("init")
    assert code == exit_codes.SUCCESS

    code, out, _ = run_cli(
        "--json", "workspace", "create", "bad name!", str(tmp_path)
    )

    assert code == exit_codes.INVALID_ARGUMENT
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
