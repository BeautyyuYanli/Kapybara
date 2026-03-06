"""Filesystem sidecar index helpers for :mod:`k.agent.memory.folder`.

This subpackage contains the sidecar-index/caching mechanics used by
`FolderMemoryStore` so the main store module can stay focused on
serialization/query behavior.
"""

from k.agent.memory.folder_store.sidecar.index import FolderSidecarIndexMixin

__all__ = ["FolderSidecarIndexMixin"]
