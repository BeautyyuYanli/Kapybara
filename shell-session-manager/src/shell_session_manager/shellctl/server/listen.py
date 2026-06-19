"""Listen-address parsing shared by shellctl transport entrypoints."""

from __future__ import annotations

import typer


def parse_listen(value: str) -> tuple[str, int]:
    """Parse a `host:port` listener string used by shellctl server commands."""

    if ":" not in value:
        raise typer.BadParameter("listen must use host:port format")
    host, raw_port = value.rsplit(":", 1)
    host = host.strip("[]")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid port: {raw_port}") from exc
    return host, port


__all__ = ["parse_listen"]
