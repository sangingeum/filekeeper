"""A transient workspace keeps one identity across processes.

With global state the journal is keyed by workspace id, so a random id per
process would hide one process's work from the next. These tests start real,
separate processes against the same root and read history across them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run(home: Path, root: Path, *arguments: str) -> tuple[int, dict[str, object]]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "filekeeper.cli.app",
            "--home",
            str(home),
            "--root",
            str(root),
            "--json",
            *arguments,
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.stdout.strip(), completed.stderr
    return completed.returncode, json.loads(completed.stdout)


def test_a_second_process_sees_and_can_undo_the_first_process_work(
    home: Path, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    home.mkdir(parents=True)
    (home / "config.toml").write_text('[filekeeper]\nstate_location = "global"\n', encoding="utf-8")

    created_code, created = _run(home, root, "file", "create", "one.txt", "--content", "first\n")
    assert created_code == 0, created
    operation_id = str(created["operation_id"])

    history_code, history = _run(home, root, "history", "list")
    assert history_code == 0, history
    assert history["count"] == 1, history
    operations = history["operations"]
    assert isinstance(operations, list)
    assert operations[0]["operation_id"] == operation_id
    assert operations[0]["workspace_id"].startswith("ws-transient-")

    undo_code, undone = _run(home, root, "history", "undo", operation_id)
    assert undo_code == 0, undone
    assert undone["ok"] is True
    assert not (root / "one.txt").exists()


def test_the_identity_is_derived_from_the_resolved_root(home: Path, tmp_path: Path) -> None:
    from filekeeper.core.service import transient_workspace_id

    root = tmp_path / "workspace"
    root.mkdir()
    same = tmp_path / "workspace" / ".." / "workspace"

    assert transient_workspace_id(root) == transient_workspace_id(same)
    assert transient_workspace_id(root) != transient_workspace_id(tmp_path)
