"""Import Core with the MCP SDK made unimportable.

Run as a subprocess by the test suite. Any attempt to import ``mcp`` or a
submodule of it fails loudly, proving that Core (and the CLI) do not depend on the
MCP SDK being installed.
"""

from __future__ import annotations

import importlib.abc
import sys


class BlockMcpFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "mcp" or fullname.startswith("mcp."):
            raise ImportError(f"the MCP SDK is unavailable in this probe: {fullname}")
        return


sys.meta_path.insert(0, BlockMcpFinder())

import filekeeper  # noqa: E402
import filekeeper.cli.app  # noqa: E402
import filekeeper.core.service  # noqa: E402

try:
    import mcp  # noqa: F401
except ImportError:
    pass
else:  # pragma: no cover - only if the SDK is genuinely installed
    print("mcp-importable", file=sys.stderr)
    raise SystemExit(3)

print(f"core-ok {filekeeper.__version__}")
