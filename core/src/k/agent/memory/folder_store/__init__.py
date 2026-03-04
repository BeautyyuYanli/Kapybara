"""FolderMemoryStore sidecar-index internals.

This package groups helper modules used by `k.agent.memory.folder` so the
public store implementation remains focused on query/persistence semantics.
"""

from k.agent.memory.folder_store.sidecar import FolderSidecarIndexMixin

__all__ = ["FolderSidecarIndexMixin"]
