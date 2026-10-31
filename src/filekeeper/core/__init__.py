"""Filekeeper Core: workspace-confined, crash-safe filesystem operations.

Core imports nothing outside the Python standard library. It has no knowledge of
the CLI, of the MCP server, or of any filesystem protocol.
"""

from .config import Config
from .errors import CallerSurface, ErrorCode, FilekeeperError, OverrideHint
from .history.blobs import BlobStore
from .models import (
    ContentMatch,
    ContentSearchResult,
    DirectoryEntry,
    DirectoryListing,
    ErrorDetail,
    ExistsResult,
    FileReadResult,
    MutationResult,
    OperationItemResult,
    PathInfo,
    PathSearchResult,
    ReadManyEntry,
    Workspace,
)
from .service import Filekeeper, FilekeeperService, init_home
from .workspace import WorkspaceHandle, WorkspaceRegistry

__all__ = [
    "BlobStore",
    "CallerSurface",
    "Config",
    "ContentMatch",
    "ContentSearchResult",
    "DirectoryEntry",
    "DirectoryListing",
    "ErrorCode",
    "ErrorDetail",
    "ExistsResult",
    "FileReadResult",
    "Filekeeper",
    "FilekeeperError",
    "FilekeeperService",
    "MutationResult",
    "OperationItemResult",
    "OverrideHint",
    "PathInfo",
    "PathSearchResult",
    "ReadManyEntry",
    "Workspace",
    "WorkspaceHandle",
    "WorkspaceRegistry",
    "init_home",
]
