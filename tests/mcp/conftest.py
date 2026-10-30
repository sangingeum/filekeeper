"""Fixtures for the adapter tests: an isolated home and a workspace tree.

The directory is put on ``sys.path`` so the harness module can be imported by the
test modules without turning the test tree into a package.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import ServerTarget


@pytest.fixture
def target(tmp_path: Path) -> ServerTarget:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("# nested\n", encoding="utf-8")
    return ServerTarget(root=root, home=tmp_path / "home")
