"""Select and configure registered Pydantic AI capabilities for each agent run.

Importing this module does not register plugins or create agents. Consumers own
the registry and install its dynamic entrance when constructing their Agent.
"""

from .registry import PluginRegistry
from .types import PluginDeps, PluginSpec

__all__ = ["PluginDeps", "PluginRegistry", "PluginSpec"]
