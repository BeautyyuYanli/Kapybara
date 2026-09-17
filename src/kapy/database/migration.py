"""Small Alembic adapter; callers own the connection, metadata and table scope.

Independent databases keep independent version histories. Generated revisions
contain fixed operations, never imports of live ORM model definitions.
"""

from collections.abc import Callable, Sequence
from pathlib import Path

from alembic import command, context
from alembic.config import Config
from sqlalchemy import Connection, MetaData


def migration_config(
    connection: Connection,
    *,
    directory: Path,
    metadata: MetaData | Sequence[MetaData],
    version_table: str,
    owns_table: Callable[[str], bool],
) -> Config:
    config = Config()
    config.set_main_option("script_location", str(directory))
    # Formatting is part of generation; revision authors never hand-edit output.
    config.set_section_option("post_write_hooks", "hooks", "ruff_check,ruff_format")
    for hook, options in (
        ("ruff_check", "check --fix REVISION_SCRIPT_FILENAME"),
        ("ruff_format", "format REVISION_SCRIPT_FILENAME"),
    ):
        config.set_section_option("post_write_hooks", f"{hook}.type", "module")
        config.set_section_option("post_write_hooks", f"{hook}.module", "ruff")
        config.set_section_option("post_write_hooks", f"{hook}.options", options)
    config.attributes.update(
        connection=connection,
        target_metadata=metadata,
        version_table=version_table,
        owns_table=owns_table,
    )
    return config


def run_environment() -> None:
    """Execute one component environment on its caller-supplied synchronous connection."""
    attributes = context.config.attributes
    connection: Connection = attributes["connection"]
    owns = attributes["owns_table"]
    version = attributes["version_table"]

    def include_name(name: str | None, kind: str, parents) -> bool:
        if kind == "schema":
            return name is None
        if kind == "table":
            return name is not None and name != version and owns(name)
        return True

    context.configure(
        connection=connection,
        target_metadata=attributes["target_metadata"],
        version_table=version,
        include_name=include_name,
        include_schemas=True,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def execute(config: Config, operation: str, *, message: str | None = None) -> None:
    if operation == "upgrade":
        command.upgrade(config, "head")
    elif operation == "current":
        command.current(config)
    elif operation == "revision":
        if not message:
            raise ValueError("revision requires --message")
        command.revision(config, message=message, autogenerate=True)
    else:
        raise ValueError("Unknown migration operation")
