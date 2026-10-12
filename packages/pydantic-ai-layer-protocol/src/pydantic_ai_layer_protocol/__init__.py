"""Typed layer protocol independent of application storage and business scopes.

Temporal State transport is an optional integration in ``.temporal``; importing
the core package never loads that integration or requires the Temporal SDK.
"""

from .registry import LayerRegistry, dump_object
from .scopes import RetryableScopeError
from .types import JsonObject, Layer, LayerAccess, LayerData, LayerDeps, LayerFactory

__all__ = [
    "JsonObject",
    "Layer",
    "LayerAccess",
    "LayerData",
    "LayerDeps",
    "LayerFactory",
    "LayerRegistry",
    "RetryableScopeError",
    "dump_object",
]
