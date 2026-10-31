"""STDIO entry point for the Filekeeper MCP server.

The server is configuration-bound: the workspace root, the audit identity and
the log level all come from start-up options, never from a tool argument. Tools
are thin wrappers around :mod:`filekeeper.core.service`; this module only parses
start-up options, builds the protocol server and runs it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from typing import Any

import mcp.types as types
from mcp.server.mcpserver import MCPServer

from filekeeper import Filekeeper

from . import __version__
from .tools import (
    TOOL_BINDINGS,
    ArgumentAwareTool,
    FilekeeperTools,
    WorkspaceSelection,
)

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Filekeeper performs confined, atomic filesystem operations inside one workspace.
Paths are workspace-relative. Line numbers are 1-based and inclusive. Mutating
tools return operation_id and undo_available; pass expected_hash (whole file) or
expected_content (edited region) when you did not just read the target, and use
dry_run to validate without writing. Undo is safe: it refuses with UNDO_CONFLICT
rather than overwrite a later change.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="filekeeper-mcp",
        description=(
            "Serve Filekeeper's filesystem operations over the Model Context "
            "Protocol on standard input/output. The workspace is fixed at start-up."
        ),
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--workspace",
        metavar="NAME",
        help="registered workspace name or id to serve (default: the configured default)",
    )
    selection.add_argument(
        "--root",
        metavar="PATH",
        help="serve this directory as a transient workspace (nothing is registered)",
    )
    parser.add_argument(
        "--home",
        metavar="PATH",
        help="Filekeeper home directory (default: FILEKEEPER_HOME or the user data dir)",
    )
    parser.add_argument(
        "--actor",
        metavar="NAME",
        help="identity recorded on every mutation; optional audit metadata",
    )
    parser.add_argument(
        "--task-id",
        metavar="ID",
        help="optional external task identifier recorded on every mutation",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["debug", "info", "warning", "error", "critical"],
        help="stderr log level (default: warning); stdout carries protocol messages only",
    )
    parser.add_argument("--version", action="version", version=f"filekeeper-mcp {__version__}")
    return parser


def build_server(
    *,
    workspace: str | None = None,
    root: str | None = None,
    home: str | None = None,
    actor: str | None = None,
    task_id: str | None = None,
    log_level: str = "WARNING",
) -> MCPServer[Any]:
    """Build the protocol server without running it (used by tests and main)."""

    app = Filekeeper(home=home)
    selection = WorkspaceSelection(app, workspace=workspace, root=root)
    if selection.selected_name() is None:
        logger.warning("starting without a selected workspace; workspace-scoped tools will refuse")
    tools = FilekeeperTools(selection, actor=actor, task_id=task_id)

    server: MCPServer[Any] = MCPServer(
        "filekeeper",
        title="Filekeeper",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level=log_level,  # type: ignore[arg-type]
    )
    for name, hints in TOOL_BINDINGS:
        _register_tool(server, getattr(tools, name), name=name, hints=hints)
    return server


def _register_tool(
    server: MCPServer[Any],
    function: Callable[..., Any],
    *,
    name: str,
    hints: types.ToolAnnotations,
) -> None:
    """Register one tool, keeping argument-validation failures coded.

    ``MCPServer.add_tool`` accepts only a plain function and builds the SDK's
    base ``Tool`` for it, so the argument-aware subclass is inserted into the
    tool manager directly. That layout is pinned by ``mcp>=2.3,<3`` and covered
    by a protocol test: the coded envelope fails loudly if the SDK moves.
    """

    tool = ArgumentAwareTool.from_function(
        function, name=name, annotations=hints, structured_output=False
    )
    schema = dict(tool.parameters)
    # The published schema says what the subclass enforces: unknown arguments are
    # refused instead of silently ignored.
    schema["additionalProperties"] = False
    tool.parameters = schema
    server._tool_manager._tools[tool.name] = tool


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = str(args.log_level).upper()
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logger.setLevel(level)

    try:
        server = build_server(
            workspace=args.workspace,
            root=args.root,
            home=args.home,
            actor=args.actor,
            task_id=args.task_id,
            log_level=level,
        )
    except Exception as error:  # start-up only: report and leave, never serve half-built
        sys.stderr.write(f"filekeeper-mcp: cannot start: {type(error).__name__}\n")
        return 2

    server.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry point
    sys.exit(main())
