"""Filesystem operations, grouped by concern.

``edit`` is a published stub for a later release; importing it is safe and every
call raises rather than pretending to work.
"""

from . import directory, edit, read, search, write

__all__ = ["directory", "edit", "read", "search", "write"]
