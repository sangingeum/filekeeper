"""MCP protocol adapter over Filekeeper Core.

This package contains no filesystem logic. Every operation is delegated to
``filekeeper.core``; the adapter only maps protocol arguments to Core calls and
Core results to structured protocol results. Core remains importable without the
MCP SDK installed.
"""

from __future__ import annotations

__version__ = "0.1.2"

__all__ = ["__version__"]
