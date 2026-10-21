"""Serializable access configuration and per-create native specifications.

Constructing, validating or serializing these data objects performs no I/O.
Serialization includes credentials; repr omits them. Native provider schemas
remain provider-owned rather than becoming a portable SDK policy language.
"""

from __future__ import annotations

import json
import math
import types
from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar, Union, get_args, get_origin, get_type_hints

from ._native import InvalidArgumentError


def _invalid(message: str) -> InvalidArgumentError:
    error = InvalidArgumentError(message)
    error.backend = "python"
    error.operation = "configure"
    error.kind = "InvalidArgument"
    return error


def _matches(value: Any, annotation: Any) -> bool:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if annotation is Any:
        return True
    if origin in (Union, types.UnionType):
        return any(_matches(value, item) for item in args)
    if origin is list:
        return isinstance(value, list) and all(_matches(item, args[0]) for item in value)
    if origin is dict:
        return isinstance(value, dict) and all(
            _matches(key, args[0]) and _matches(item, args[1]) for key, item in value.items()
        )
    if annotation is float:
        if type(value) not in (float, int):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    return type(value) is annotation


class _Data:
    backend: ClassVar[str]
    _section: ClassVar[str]

    def __post_init__(self) -> None:
        if not isinstance(getattr(type(self), "backend", None), str):
            raise _invalid("use a concrete provider configuration or specification")
        hints = get_type_hints(type(self))
        for item in fields(self):
            if not _matches(getattr(self, item.name), hints[item.name]):
                raise _invalid(f"invalid {type(self).__name__}.{item.name} type")
        if hasattr(self, "request_timeout") and self.request_timeout <= 0:
            raise _invalid("request_timeout must be finite and positive")
        if isinstance(self, SandboxSpec):
            if self.name is not None and (not self.name or "\0" in self.name):
                raise _invalid("name must be nonempty and contain no NUL")
            if any(not key or "\0" in key or "\0" in value for key, value in self.labels.items()):
                raise _invalid("invalid labels")
        if isinstance(self, _GrpcConfig) and ((self.client_certificate is None) != (self.client_key is None)):
            raise _invalid("client_certificate and client_key must be provided together")
        if isinstance(self, OpenShellSandboxSpec):
            if (self.workload_template is None) == (self.spec_protobuf is None):
                raise _invalid("choose exactly one workload_template or spec_protobuf")

    def to_dict(self) -> dict[str, Any]:
        """Return independent data, including configured credentials."""
        self.__post_init__()
        data = asdict(self)
        if isinstance(self, OpenShellSandboxSpec) and self.spec_protobuf is not None:
            data["spec_protobuf"] = list(self.spec_protobuf)
        return {"backend": self.backend, self._section: data}

    def to_json(self) -> str:
        try:
            return json.dumps(self.to_dict(), allow_nan=False)
        except (TypeError, ValueError, OverflowError) as error:
            raise _invalid("configuration/specification must contain valid JSON data") from error

    @classmethod
    def from_dict(cls, value: dict[str, Any]):
        """Restore the explicit data kind; this never opens a Backend or selects a target."""
        try:
            if set(value) != {"backend", cls._section}:
                raise ValueError
            candidates = _CONFIGS if cls._section == "config" else _SPECS
            selected = next(item for item in candidates if item.backend == value["backend"])
            if cls not in (BackendConfig, SandboxSpec) and selected is not cls:
                raise ValueError
            data = dict(value[cls._section])
            if selected is OpenShellSandboxSpec and data.get("spec_protobuf") is not None:
                raw = data["spec_protobuf"]
                if not isinstance(raw, list) or any(type(item) is not int or not 0 <= item <= 255 for item in raw):
                    raise ValueError
                data["spec_protobuf"] = bytes(raw)
            return selected(**data)
        except (KeyError, TypeError, ValueError, StopIteration) as error:
            raise _invalid("invalid serialized configuration/specification") from error

    @classmethod
    def from_json(cls, value: str):
        try:
            return cls.from_dict(json.loads(value))
        except (TypeError, ValueError) as error:
            raise _invalid("invalid serialized configuration/specification") from error


@dataclass(frozen=True, kw_only=True)
class BackendConfig(_Data):
    """Access data only. Use a concrete provider configuration below."""
    _section: ClassVar[str] = "config"


@dataclass(frozen=True)
class LocalConfig(BackendConfig):
    backend: ClassVar[str] = "local"


@dataclass(frozen=True)
class SshConfig(BackendConfig):
    destination: str
    port: int | None = None
    identity_file: str | None = None
    config_file: str | None = None
    program: str = "ssh"
    backend: ClassVar[str] = "ssh"


@dataclass(frozen=True)
class DockerConfig(BackendConfig):
    endpoint: str | None = None
    backend: ClassVar[str] = "docker"


@dataclass(frozen=True)
class E2bConfig(BackendConfig):
    api_key: str = field(repr=False)
    api_endpoint: str = "https://api.e2b.app"
    request_timeout: float = 30.0
    sandbox_domain: str = "e2b.app"
    envd_endpoint: str | None = None
    backend: ClassVar[str] = "e2b"


@dataclass(frozen=True)
class EnvdConfig(BackendConfig):
    endpoint: str
    access_token: str | None = field(default=None, repr=False)
    user: str | None = None
    sandbox_id: str | None = None
    request_timeout: float = 30.0
    backend: ClassVar[str] = "envd"


@dataclass(frozen=True)
class AgentSandboxConfig(BackendConfig):
    namespace: str = "default"
    context: str | None = None
    kubeconfig: str | None = None
    program: str = "kubectl"
    request_timeout: float = 30.0
    backend: ClassVar[str] = "agent-sandbox"


@dataclass(frozen=True)
class PueueConfig(BackendConfig):
    config_file: str | None = None
    profile: str | None = None
    group: str | None = None
    pueue_directory: str | None = None
    program: str = "pueue"
    request_timeout: float = 30.0
    backend: ClassVar[str] = "pueue"


@dataclass(frozen=True)
class TmuxConfig(BackendConfig):
    socket_path: str
    session_prefix: str = "opensol"
    program: str = "tmux"
    request_timeout: float = 30.0
    backend: ClassVar[str] = "tmux"


@dataclass(frozen=True)
class _GrpcConfig(BackendConfig):
    endpoint: str
    bearer_token: str | None = field(default=None, repr=False)
    ca_certificate: str | None = field(default=None, repr=False)
    client_certificate: str | None = field(default=None, repr=False)
    client_key: str | None = field(default=None, repr=False)
    request_timeout: float = 30.0


@dataclass(frozen=True)
class SandboxdConfig(_GrpcConfig):
    backend: ClassVar[str] = "sandboxd"


@dataclass(frozen=True)
class OpenShellConfig(_GrpcConfig):
    workspace: str = "default"
    backend: ClassVar[str] = "openshell"


@dataclass(frozen=True, kw_only=True)
class SandboxSpec(_Data):
    """One complete create request; never retained by a manager."""
    name: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    _section: ClassVar[str] = "spec"


@dataclass(frozen=True)
class DockerSandboxSpec(SandboxSpec):
    image: str
    command: list[str] = field(default_factory=list, repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    working_dir: str | None = None
    host_config: dict[str, Any] | None = field(default=None, repr=False)
    backend: ClassVar[str] = "docker"


@dataclass(frozen=True)
class E2bSandboxSpec(SandboxSpec):
    template: str = "base"
    sandbox_timeout: int = 300
    auto_pause: bool = False
    env: dict[str, str] = field(default_factory=dict, repr=False)
    metadata: dict[str, str] = field(default_factory=dict, repr=False)
    backend: ClassVar[str] = "e2b"


@dataclass(frozen=True)
class AgentSandboxSpec(SandboxSpec):
    pod_template: dict[str, Any] = field(repr=False)
    volume_claim_templates: list[dict[str, Any]] = field(default_factory=list, repr=False)
    service: bool = True
    backend: ClassVar[str] = "agent-sandbox"


@dataclass(frozen=True)
class OpenShellSandboxSpec(SandboxSpec):
    workload_template: str | None = None
    spec_protobuf: bytes | None = field(default=None, repr=False)
    backend: ClassVar[str] = "openshell"


_CONFIGS = (LocalConfig, SshConfig, DockerConfig, E2bConfig, EnvdConfig, AgentSandboxConfig,
            PueueConfig, TmuxConfig, SandboxdConfig, OpenShellConfig)
_SPECS = (DockerSandboxSpec, E2bSandboxSpec, AgentSandboxSpec, OpenShellSandboxSpec)


class _RuntimeBackend:
    """Shared marker for sync/async handles to the same native context."""
    def __init__(self, native) -> None:
        self._native = native

    @property
    def backend(self) -> str:
        return self._native.backend


class _RuntimeConnection:
    def __init__(self, native) -> None:
        self._native = native


def _source(source):
    if isinstance(source, _RuntimeBackend):
        return source._native
    if type(source) in _CONFIGS:
        return source.to_json()
    raise _invalid("source must be a BackendConfig or an open Backend")


def _connection(connection):
    if connection is None:
        return None
    if not isinstance(connection, _RuntimeConnection):
        raise _invalid("connection must be a public Connection")
    native = connection._native
    return native._blocking() if hasattr(native, "_blocking") else native


def _target(target):
    from ._native import SandboxRef
    if target is not None and not isinstance(target, SandboxRef):
        raise _invalid("target must be a SandboxRef")
    return target


def _spec(spec: SandboxSpec) -> str:
    if type(spec) not in _SPECS:
        raise _invalid("create requires a concrete SandboxSpec")
    return spec.to_json()


__all__ = ["BackendConfig", "SandboxSpec", *[item.__name__ for item in _CONFIGS + _SPECS]]
