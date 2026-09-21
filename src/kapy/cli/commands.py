"""Dispatch CLI arguments without opening resources or importing inactive interfaces.

Each selected interface or database command owns its argument parsing and lifecycle.
The archived prototype CLI is independent of this entry point.
"""

import typer

app = typer.Typer(no_args_is_help=True)


@app.command(
    "interface",
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        "allow_interspersed_args": False,
    },
)
def interface_command(ctx: typer.Context, name: str) -> None:
    """Run one interface; remaining arguments belong to its entry point."""
    from kapy.cli.registry import INTERFACE_ENTRIES, dispatch

    if name not in INTERFACE_ENTRIES:
        raise typer.BadParameter(f"Unknown interface; choose: {', '.join(INTERFACE_ENTRIES)}")
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
    """Upgrade or inspect the core PostgreSQL schema independently of interfaces."""
    from kapy.database.schema import main as database_main

    raise typer.Exit(database_main([operation, *ctx.args]))


def main() -> None:
    app()
