"""Legacy names for the shared shell JSON configuration and state models."""

from kapy.layers.shell.models import (
    Config as ShellPluginConfig,
)
from kapy.layers.shell.models import (
    NonEmpty,
    RunTimeout,
    WaitTimeout,
)
from kapy.layers.shell.models import (
    State as ShellPluginState,
)

__all__ = ["NonEmpty", "RunTimeout", "WaitTimeout", "ShellPluginConfig", "ShellPluginState"]
