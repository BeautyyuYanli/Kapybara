"""Lazy CLI entries, not a process supervisor or runtime resource registry."""

from collections.abc import Callable
from importlib import import_module
from typing import cast

INTERFACE_ENTRIES = {
    "http": "kapy.interfaces.http.main:main",
    "telegram": "kapy.interfaces.telegram.main:main",
}


def dispatch(name: str, argv: list[str]) -> int:
    """Run exactly one selected entry in the current process, forwarding arguments intact."""
    if name not in INTERFACE_ENTRIES:
        raise ValueError(f"Unknown interface {name!r}; choose: {', '.join(INTERFACE_ENTRIES)}")
    module, attribute = INTERFACE_ENTRIES[name].split(":", 1)
    entry = cast(Callable[[list[str]], int], getattr(import_module(module), attribute))
    return entry(argv)
