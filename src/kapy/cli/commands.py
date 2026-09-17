"""Dispatch CLI arguments without opening resources or importing inactive plugins.

Each selected plugin or database command owns its argument parsing and lifecycle.
The archived prototype CLI is independent of this entry point.
"""

import typer

app = typer.Typer(no_args_is_help=True)


@app.command(
    "plugin",
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "allow_interspersed_args": False,
    },
)
def plugin_command(ctx: typer.Context, name: str) -> None:
    """Run one interface plugin; remaining arguments belong to its entry point."""
    from kapy.plugins.registry import PLUGIN_ENTRIES, dispatch

    if name not in PLUGIN_ENTRIES:
        raise typer.BadParameter(f"Unknown plugin; choose: {', '.join(PLUGIN_ENTRIES)}")
    raise typer.Exit(dispatch(name, list(ctx.args)))


@app.command(
    "db",
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "allow_interspersed_args": False,
    },
)
def database_command(ctx: typer.Context, operation: str) -> None:
    """Upgrade or inspect the core PostgreSQL schema independently of plugins."""
    from kapy.database.schema import main as database_main

    raise typer.Exit(database_main([operation, *ctx.args]))


def main() -> None:
    app()
