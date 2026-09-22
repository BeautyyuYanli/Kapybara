"""The builtin.shell definition; registration does not automatically bind sessions."""

from kapy.agent_plugins.registry import PluginDefinition

from .models import ShellPluginConfig, ShellPluginState
from .plugin import ShellPlugin

SHELL_PLUGIN = PluginDefinition(
    plugin_provider="builtin",
    plugin_name="shell",
    data_version=1,
    config_type=ShellPluginConfig,
    state_type=ShellPluginState,
    plugin_type=ShellPlugin,
)

__all__ = ["SHELL_PLUGIN", "ShellPlugin", "ShellPluginConfig", "ShellPluginState"]
