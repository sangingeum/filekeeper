"""The CLI and the MCP server must make the same decision and the same bytes.

Two identical trees, one operation each: a guarded write that succeeds and a
guarded write whose hash is stale. The filesystem result and the refusal code are
compared directly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from harness import ServerTarget, exercise


def _tree(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    return root


def _cli_json(run_cli: Callable[..., tuple[int, str, str]], *args: str) -> tuple[int, dict]:
    code, out, _err = run_cli(*args)
    return code, json.loads(out)


def test_a_guarded_write_produces_the_same_bytes_through_both_adapters(
    tmp_path: Path, home: Path, run_cli: Callable[..., tuple[int, str, str]]
) -> None:
    cli_root = _tree(tmp_path, "cli")
    mcp_root = _tree(tmp_path, "mcp")

    code, info = _cli_json(run_cli, "file", "info", "notes.txt", "--root", str(cli_root), "--json")
    assert code == 0
    expected_hash = info["content_hash"]

    cli_code, cli_payload = _cli_json(
        run_cli,
        "file",
        "write",
        "notes.txt",
        "--content",
        "alpha\nBETA\ngamma\n",
        "--expected-hash",
        expected_hash,
        "--root",
        str(cli_root),
        "--json",
    )

    mcp = exercise(
        ServerTarget(root=mcp_root, home=home),
        [
            (
                "file_write",
                {
                    "path": "notes.txt",
                    "content": "alpha\nBETA\ngamma\n",
                    "expected_hash": expected_hash,
                },
            )
        ],
    )
    mcp_payload = mcp.by_name("file_write").structured

    assert cli_code == 0
    assert mcp_payload["ok"] is True
    assert (cli_root / "notes.txt").read_bytes() == (mcp_root / "notes.txt").read_bytes()
    assert cli_payload["after_hash"] == mcp_payload["after_hash"]
    assert cli_payload["before_hash"] == mcp_payload["before_hash"]
    assert cli_payload["undo_available"] is True
    assert mcp_payload["undo_available"] is True
    assert cli_payload["operation_id"].startswith("op_")
    assert mcp_payload["operation_id"].startswith("op_")


def test_a_stale_hash_is_the_same_refusal_through_both_adapters(
    tmp_path: Path, home: Path, run_cli: Callable[..., tuple[int, str, str]]
) -> None:
    cli_root = _tree(tmp_path, "cli")
    mcp_root = _tree(tmp_path, "mcp")
    stale = "sha256:" + "0" * 64

    cli_code, cli_payload = _cli_json(
        run_cli,
        "file",
        "write",
        "notes.txt",
        "--content",
        "replaced\n",
        "--expected-hash",
        stale,
        "--root",
        str(cli_root),
        "--json",
    )

    mcp = exercise(
        ServerTarget(root=mcp_root, home=home),
        [
            (
                "file_write",
                {"path": "notes.txt", "content": "replaced\n", "expected_hash": stale},
            )
        ],
    )
    refusal = mcp.by_name("file_write")

    assert cli_code == 5
    assert cli_payload["ok"] is False
    assert cli_payload["error"]["code"] == "CONTENT_CONFLICT"
    assert refusal.is_error is True
    assert refusal.code == "CONTENT_CONFLICT"
    assert refusal.error["retryable"] is True
    assert cli_payload["error"]["retryable"] is True
    assert (cli_root / "notes.txt").read_bytes() == (mcp_root / "notes.txt").read_bytes()
