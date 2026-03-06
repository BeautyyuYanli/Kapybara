"""Composed public sidecar mixin used by `FolderMemoryStore`."""

from __future__ import annotations

from pathlib import Path

from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder_store.sidecar.base import FolderSidecarBaseMixin
from k.agent.memory.folder_store.sidecar.drift import FolderSidecarDriftMixin
from k.agent.memory.folder_store.sidecar.metadata import FolderSidecarMetadataMixin
from k.agent.memory.folder_store.sidecar.stats import FolderSidecarStatsMixin


class FolderSidecarIndexMixin(
    FolderSidecarBaseMixin,
    FolderSidecarMetadataMixin,
    FolderSidecarDriftMixin,
    FolderSidecarStatsMixin,
):
    """Provide filesystem sidecar-index lifecycle for `FolderMemoryStore`."""

    # Hooks expected from FolderMemoryStore
    def _list_loadable_record_paths(self) -> list[Path]:  # pragma: no cover
        raise NotImplementedError

    def _load_record_from_record_path(
        self, record_path: Path
    ) -> MemoryRecord:  # pragma: no cover
        raise NotImplementedError

    def _persist_record(
        self, record: MemoryRecord, *, path_hint: Path | None
    ) -> Path:  # pragma: no cover
        raise NotImplementedError

    def _repair_missing_links(
        self,
        *,
        records: list[MemoryRecord],
        by_id: dict[str, MemoryRecord],
        missing_record_ids: set[str],
    ) -> set[str]:  # pragma: no cover
        raise NotImplementedError

    def _expected_id_for_record_path(self, path: Path) -> str:  # pragma: no cover
        raise NotImplementedError

    def _detailed_path_for_record_path(
        self, record_path: Path
    ) -> Path:  # pragma: no cover
        raise NotImplementedError

    def _atomic_write_text(self, path: Path, text: str) -> None:  # pragma: no cover
        raise NotImplementedError
