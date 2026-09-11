"""Telegram migrations use only its private SQLite file and version history."""

from pathlib import Path

from kapy.tmpv2.database.migration import execute, migration_config

from .models import TelegramTable
from .storage import open_storage


async def migrate(path: Path, operation: str, message: str | None = None) -> None:
    async with open_storage(path, create=operation in {"upgrade", "revision"}) as engine:
        async with engine.begin() as connection:

            def run(db):
                config = migration_config(
                    db,
                    directory=Path(__file__).parent / "migrations",
                    metadata=TelegramTable.metadata,
                    version_table="plugin_telegram_schema_version",
                    owns_table=lambda name: name.startswith("plugin_telegram_"),
                )
                execute(config, operation, message=message)

            await connection.run_sync(run)
