"""Delete physical output first, then its deleting row. No execution or scheduling ownership."""

import logging
import shutil
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from ._io import run_io
from .repository import ProcessRepository
from .types import ProcessError

logger = logging.getLogger(__name__)


async def gc_processes(
    repository: ProcessRepository,
    output_dir: Path,
    *,
    limit: int = 100,
) -> int:
    if not 1 <= limit <= 200:
        raise ProcessError("invalid_argument", "limit must be between 1 and 200")
    deleted = 0
    for process_id in await repository.deleting(limit):
        root = output_dir / str(process_id)

        def remove(root: Path = root) -> None:
            try:
                shutil.rmtree(root)
            except FileNotFoundError:
                pass

        try:
            await run_io(remove)
            deleted += await repository.delete(process_id)
        except OSError, SQLAlchemyError:
            # Keep deletion intent for the next sweep.
            logger.warning("Process resource cleanup failed: %s", process_id)
    return deleted
