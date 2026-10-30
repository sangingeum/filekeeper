"""Prove Core is importable and usable with the MCP SDK unimportable.

The probe runs in a subprocess whose import machinery blocks ``mcp`` outright, so
this is a real test of the dependency boundary rather than an assumption that no
import statement exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROBE = PROJECT_ROOT / "tests" / "helpers" / "no_mcp_probe.py"


def test_core_imports_without_mcp_sdk() -> None:
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src") + (
        os.pathsep + existing if existing else ""
    )

    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "core-ok" in completed.stdout
    assert "mcp-importable" not in completed.stderr
