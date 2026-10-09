"""RPC plugins for Pydantic AI, independent of Kapy's application services."""

from .capability import PluginHostCapability, build_plugin_host, describe_plugins, load_plugin_host
from .contracts import (
    HookOrderingSpec,
    HookPoint,
    HookSpec,
    InstructionSpec,
    InvokeReply,
    KeepReply,
    PluginHostError,
    PluginManifest,
    RpcInstructionSpec,
    TextInstructionSpec,
    UnhandledReply,
    ValueReply,
)
from .rpc import RpcClient

__all__ = [
    "HookOrderingSpec",
    "HookPoint",
    "HookSpec",
    "InstructionSpec",
    "InvokeReply",
    "KeepReply",
    "PluginHostCapability",
    "PluginHostError",
    "PluginManifest",
    "RpcClient",
    "RpcInstructionSpec",
    "TextInstructionSpec",
    "UnhandledReply",
    "ValueReply",
    "build_plugin_host",
    "describe_plugins",
    "load_plugin_host",
]
