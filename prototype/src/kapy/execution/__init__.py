"""Machine execution and local proxy interfaces."""

from .client import call_local_proxy
from .config import DaemonConfig
from .daemon import run_daemon
from .paths import ExecutionPaths, resolve_paths
from .types import ProxyAuth, SessionProxyAuth, UserProxyAuth

__all__ = [
    "DaemonConfig",
    "ExecutionPaths",
    "ProxyAuth",
    "SessionProxyAuth",
    "UserProxyAuth",
    "call_local_proxy",
    "resolve_paths",
    "run_daemon",
]
