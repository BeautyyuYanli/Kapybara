"""CLI laziness and database commands stay independent of bot/core credentials."""

import os
import subprocess
import sys

from typer.testing import CliRunner

from kapy.cli.commands import app


def test_plugin_help_is_forwarded_and_unknown_names_are_rejected():
    runner = CliRunner()
    result = runner.invoke(app, ["plugin", "telegram", "serve", "--help"])
    assert result.exit_code == 0 and "--database-path" in result.output
    result = runner.invoke(app, ["plugin", "unknown", "serve"])
    assert result.exit_code == 2 and "http, telegram" in result.output


def test_registry_and_cli_help_do_not_import_plugins():
    code = """
import sys
from typer.testing import CliRunner
from kapy.cli.commands import app
from kapy.tmpv2.plugins.registry import PLUGIN_ENTRIES
assert CliRunner().invoke(app, ['--help']).exit_code == 0
assert set(PLUGIN_ENTRIES) == {'http', 'telegram'}
assert not any(
    name.startswith(('kapy.tmpv2.plugins.http', 'kapy.tmpv2.plugins.telegram'))
    for name in sys.modules
)
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)


def test_database_cli_uses_xdg_without_other_configuration(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("KAPY_", "TELEGRAM_"))
    }
    env.update(XDG_STATE_HOME=str(tmp_path), KAPY_DATABASE_URL="deliberately-invalid")
    command = [sys.executable, "-m", "kapy.tmpv2.plugins.telegram.main", "db"]
    subprocess.run([*command, "upgrade"], env=env, check=True, capture_output=True)
    result = subprocess.run(
        [*command, "current"], env=env, check=True, capture_output=True, text=True
    )
    assert "(head)" in result.stdout
    assert (tmp_path / "kapy/plugins/telegram/telegram.sqlite3").is_file()
