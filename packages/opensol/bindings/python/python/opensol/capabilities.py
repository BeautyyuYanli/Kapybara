"""Typed local capability descriptions shared by synchronous and asynchronous clients.

Operation support does not prove native availability, permission, state or retention.
Every query returns independent ordinary dicts/lists, without target I/O.
"""

import builtins
from typing import Literal, TypedDict

TargetRequirement = Literal["forbidden", "required"]
ConnectionDependency = Literal["forbidden", "required"]
InputContent = Literal["bytes", "utf8_without_nul"]
ExecCancellation = Literal["request_target_termination", "no_target_termination_guarantee"]
KillEffect = Literal["request_termination", "remove_task"]
CompletionKind = Literal["os_exit", "native_task"]
OutputChannel = Literal["stdout", "stderr", "terminal", "combined"]
Signal = Literal[2, 9, 15]


class BackendCapabilities(TypedDict):
    sandboxes: TargetRequirement | None
    connection: TargetRequirement | None
    processes: TargetRequirement | None
    connection_dependency: ConnectionDependency


class StopCapabilities(TypedDict):
    preserves_memory: bool


class SandboxCapabilities(TypedDict):
    inspect: bool
    start: bool
    stop: StopCapabilities | None
    delete: bool
    fork: bool


class SandboxManagerCapabilities(TypedDict):
    list: bool
    create: bool
    sandbox: SandboxCapabilities


class InputConstraints(TypedDict):
    content: InputContent
    max_bytes: int | None


class ConnectionCapabilities(TypedDict):
    stdin: InputConstraints | None
    timeout: bool
    separate_stderr: bool
    cancellation: ExecCancellation


class SpawnMode(TypedDict):
    pty: bool
    stdin: bool


class ProcessInputCapabilities(TypedDict):
    constraints: InputConstraints
    close: bool


class ProcessCapabilities(TypedDict):
    pause: bool
    start: bool
    restart: bool
    kill: KillEffect | None
    remove: bool
    input: ProcessInputCapabilities | None
    resize: bool
    supported_signals: list[Signal]
    detached: bool
    reconnect: bool
    daemon_restart_recovery: bool
    completion: CompletionKind


class SubscriptionDelivery(TypedDict):
    channels: list[OutputChannel]
    historical_output: bool
    streaming_output: bool
    completion: CompletionKind


class SubscriptionSources(TypedDict):
    initial: SubscriptionDelivery | None
    reconnect: SubscriptionDelivery | None


class OutputCapabilities(TypedDict):
    read: bool
    tail: bool
    read_channels: list[OutputChannel]
    subscribe: SubscriptionSources


class TerminalCapabilities(TypedDict):
    read: bool
    tail: bool
    screen: bool


class ProcessManagerCapabilities(TypedDict):
    probe: bool
    list: bool
    spawn_modes: builtins.list[SpawnMode]
    process: ProcessCapabilities
    output: OutputCapabilities
    terminal: TerminalCapabilities


__all__ = [
    "BackendCapabilities", "TargetRequirement", "ConnectionDependency",
    "SandboxManagerCapabilities", "SandboxCapabilities", "StopCapabilities",
    "InputContent", "InputConstraints", "ExecCancellation", "ConnectionCapabilities",
    "SpawnMode", "ProcessManagerCapabilities", "ProcessCapabilities", "ProcessInputCapabilities",
    "KillEffect", "CompletionKind", "Signal", "OutputChannel", "SubscriptionDelivery",
    "SubscriptionSources", "OutputCapabilities", "TerminalCapabilities",
]
