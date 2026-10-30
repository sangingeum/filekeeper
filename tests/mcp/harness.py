"""Real stdio sessions against the adapter, for the protocol-level tests.

Every test here spawns the server as a subprocess and talks to it through the
MCP client, so the handshake, schema publication and error envelopes are
exercised on the wire rather than by calling tool functions directly.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from mcp.client.stdio import stdio_client

from mcp import ClientSession, StdioServerParameters, types

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SOURCE = PROJECT_ROOT / "packages" / "filekeeper-mcp" / "src"


@dataclass(frozen=True)
class ToolReply:
    name: str
    structured: dict[str, Any]
    is_error: bool
    text: str

    @property
    def code(self) -> str | None:
        error = self.structured.get("error")
        if isinstance(error, dict):
            return str(error.get("code"))
        return None

    @property
    def error(self) -> dict[str, Any]:
        value = self.structured.get("error")
        assert isinstance(value, dict)
        return value


@dataclass
class SessionResult:
    tools: list[types.Tool] = field(default_factory=list)
    replies: list[ToolReply] = field(default_factory=list)

    def by_name(self, name: str) -> ToolReply:
        for reply in self.replies:
            if reply.name == name:
                return reply
        raise AssertionError(f"tool {name} was not called")


@dataclass(frozen=True)
class ServerTarget:
    root: Path | None
    home: Path

    def arguments(self, *extra: str) -> list[str]:
        arguments = [
            "--home",
            str(self.home),
            "--actor",
            "mcp-test",
            "--task-id",
            "mcp-test-1",
        ]
        if self.root is not None:
            arguments[0:0] = ["--root", str(self.root)]
        return [*arguments, *extra]


def environment() -> dict[str, str]:
    existing = os.environ.get("PYTHONPATH", "")
    path = str(PACKAGE_SOURCE) + (os.pathsep + existing if existing else "")
    return {"PYTHONPATH": path}


CallArguments = dict[str, Any] | Callable[["SessionResult"], dict[str, Any]]


def exercise(
    target: ServerTarget,
    calls: list[tuple[str, CallArguments]],
    *,
    extra_arguments: tuple[str, ...] = (),
) -> SessionResult:
    """Start one server, initialize, list tools, then run every call in order."""

    async def run() -> SessionResult:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "filekeeper_mcp.server", *target.arguments(*extra_arguments)],
            env=environment(),
            cwd=str(PROJECT_ROOT),
        )
        result = SessionResult()
        # A real file descriptor: pytest's captured stderr has no fileno().
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with stdio_client(parameters, errlog=errlog) as streams:
                read_stream, write_stream = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    result.tools = list(listing.tools)
                    for name, arguments in calls:
                        resolved = arguments(result) if callable(arguments) else arguments
                        reply = await session.call_tool(name, resolved)
                        structured = reply.structured_content
                        assert isinstance(structured, dict), (name, structured)
                        text = "".join(
                            block.text
                            for block in reply.content
                            if isinstance(block, types.TextContent)
                        )
                        result.replies.append(
                            ToolReply(
                                name=name,
                                structured=structured,
                                is_error=bool(reply.is_error),
                                text=text,
                            )
                        )
        return result

    return anyio.run(run)
