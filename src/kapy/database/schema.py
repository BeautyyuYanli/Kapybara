"""Core PostgreSQL migrations; this catalog never imports interfaces.

Keep removed/renamed table names in OWNED_TABLES so autogeneration can see their
removal. Pre-baseline development databases are discarded and initialized afresh;
upgrade deliberately does not silently stamp or skip existing tables.
"""

import argparse
import asyncio
import os
from pathlib import Path

from sqlalchemy.schema import CreateSchema

from kapy.agent_plugins import models as plugin_tables  # noqa: F401
from kapy.agent_runner.models import agent_metadata
from kapy.application.resources import open_core_database
from kapy.application.settings import CommonSettings
from kapy.control.database import ControlTable
from kapy.control.models import models as model_tables  # noqa: F401
from kapy.control.sessions import models as session_tables  # noqa: F401
from kapy.session_lease.models import lease_metadata

from .migration import execute, migration_config

OWNED_TABLES = frozenset(
    {
        "providers",
        "models",
        "sessions",
        "plugin_agent_bindings",
        "session_inputs",
        "session_cancels",
        "agent_states",
        "session_leases",
        "agent_history",
        "agent_compactions",
        "agent_context_pages",
    }
)


async def migrate(
    settings: CommonSettings,
    operation: str,
    message: str | None = None,
    *,
    plan: str | None = None,
) -> None:
    if plan is not None and (operation != "revision" or plan != "context-pages"):
        raise ValueError("context-pages is a revision generation plan")
    async with open_core_database(settings) as engine:
        async with engine.begin() as connection:
            if operation in {"upgrade", "revision"}:
                await connection.execute(CreateSchema(settings.database_schema, if_not_exists=True))

            def run(db):
                # Reflection uses the same selected schema as the unqualified core metadata.
                db.dialect.default_schema_name = settings.database_schema
                config = migration_config(
                    db,
                    directory=Path(__file__).parent / "migrations",
                    metadata=[ControlTable.metadata, agent_metadata, lease_metadata],
                    version_table="core_schema_version",
                    owns_table=OWNED_TABLES.__contains__,
                )
                if plan == "context-pages":
                    from .revision_plans import context_pages

                    config.attributes["process_revision_directives"] = context_pages
                execute(config, operation, message=message)

            await connection.run_sync(run)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kapy db")
    parser.add_argument("operation", choices=["upgrade", "current", "revision"])
    parser.add_argument("--message")
    parser.add_argument("--plan", choices=["context-pages"])
    args = parser.parse_args(argv)
    asyncio.run(
        migrate(
            CommonSettings.model_validate(dict(os.environ)),
            args.operation,
            args.message,
            plan=args.plan,
        )
    )
    return 0
