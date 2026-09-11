"""Lazy CLI entries, not a process supervisor or runtime resource registry."""

from collections.abc import Callable
from importlib import import_module
from typing import cast

PLUGIN_ENTRIES = {
    "http": "kapy.tmpv2.plugins.http.main:main",
    "telegram": "kapy.tmpv2.plugins.telegram.main:main",
}


def dispatch(name: str, argv: list[str]) -> int:
    """Run exactly one selected entry in the current process, forwarding arguments intact."""
    if name not in PLUGIN_ENTRIES:
        raise ValueError(f"Unknown plugin {name!r}; choose: {', '.join(PLUGIN_ENTRIES)}")
    module, attribute = PLUGIN_ENTRIES[name].split(":", 1)
    entry = cast(Callable[[list[str]], int], getattr(import_module(module), attribute))
    return entry(argv)
