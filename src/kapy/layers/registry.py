"""Process-local name to installed module mapping; callers never supply imports."""

from types import ModuleType

from .shell import layer as shell

REGISTRY: dict[str, ModuleType] = {"shell": shell}
