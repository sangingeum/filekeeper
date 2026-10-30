"""Core and CLI must import nothing outside the standard library.

This is a static check over the shipped source, and it is the guard that keeps the
"no third-party dependencies" promise honest as the code grows.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "filekeeper"


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_third_party_imports() -> None:
    allowed = set(sys.stdlib_module_names) | {"filekeeper"}
    offenders: dict[str, set[str]] = {}

    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        extra = _imported_roots(tree) - allowed
        if extra:
            offenders[str(path.relative_to(SOURCE_ROOT))] = extra

    assert offenders == {}, f"non-stdlib imports found: {offenders}"


def test_core_does_not_import_the_cli_or_mcp() -> None:
    for path in sorted((SOURCE_ROOT / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for module in modules:
                assert not module.startswith("filekeeper.cli"), f"{path} imports the CLI"
                assert not module.startswith("mcp"), f"{path} imports the MCP SDK"
