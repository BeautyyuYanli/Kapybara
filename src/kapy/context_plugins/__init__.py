"""Context plugin implementations and application selection registry."""

from .registry import ContextPluginRegistry, ContextPluginSpec, create_default_registry
from .summary import SummaryPlugin

__all__ = ["ContextPluginRegistry", "ContextPluginSpec", "SummaryPlugin", "create_default_registry"]
