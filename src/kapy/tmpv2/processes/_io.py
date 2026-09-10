"""Blocking filesystem work must finish before its caller closes dependent resources."""

import asyncio
from collections.abc import Callable


async def run_io[T](call: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A canceled observer cannot cancel an OS write; join it before closing its FD.
        await task
        raise
