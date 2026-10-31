"""Filekeeper: safe, predictable filesystem operations for humans and agents.

The public API is :class:`filekeeper.core.Filekeeper` for workspace registration
and :class:`filekeeper.core.FilekeeperService` for operations. The ``keeper``
command-line interface is a thin adapter over the same Core.

This release ships the foundation and the basic filesystem operations. Operation
history, undo, text editing and the MCP server are later, separately versioned
work; see ``CHANGELOG.md`` and ``README.md``.
"""

from __future__ import annotations

from .core import (
    CallerSurface,
    Config,
    ErrorCode,
    Filekeeper,
    FilekeeperError,
    FilekeeperService,
    MutationResult,
    OverrideHint,
    Workspace,
)

__version__ = "0.1.7"

__all__ = [
    "CallerSurface",
    "Config",
    "ErrorCode",
    "Filekeeper",
    "FilekeeperError",
    "FilekeeperService",
    "MutationResult",
    "OverrideHint",
    "Workspace",
    "__version__",
]
