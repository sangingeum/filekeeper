"""History foundations.

Only the content-addressed blob store exists in this release. The operation
journal, undo and conflict detection arrive in a later release; the database
schema they will use is already created and migrated.
"""

from .blobs import BlobStore

__all__ = ["BlobStore"]
