"""Shared fixtures.

Every test gets an isolated Filekeeper home and workspace; nothing touches the
developer's real state directory.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from filekeeper.core.config import Config
from filekeeper.core.service import Filekeeper

# The CLI module is imported through its package to exercise the published entry
# point exactly as the console script does.
from filekeeper.cli import main as cli_main


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "nested.md").write_text("# nested\n", encoding="utf-8")
    return root


@pytest.fixture
def app(home: Path) -> Filekeeper:
    return Filekeeper(home=home)


@pytest.fixture
def service(app: Filekeeper, workspace_root: Path):
    app.create_workspace("project", workspace_root)
    return app.workspace(name="project")


@pytest.fixture
def small_limits(home: Path, app: Filekeeper, workspace_root: Path):
    """A workspace whose read limit is small enough to force truncation."""

    app.create_workspace("small", workspace_root)
    app.config = Config(max_read_bytes=12, max_write_bytes=1024, max_directory_entries=2)
    app.registry.config = app.config
    return app.workspace(name="small")


@pytest.fixture
def run_cli(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., tuple[int, str, str]]:
    monkeypatch.setenv("FILEKEEPER_HOME", str(home))

    def run(*args: str) -> tuple[int, str, str]:
        try:
            code = cli_main(list(args))
        except SystemExit as exit_request:  # argparse --help / --version
            code = int(exit_request.code or 0)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


@pytest.fixture
def python_executable() -> str:
    return sys.executable
