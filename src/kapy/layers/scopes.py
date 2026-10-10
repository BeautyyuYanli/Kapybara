"""Business-scope retries, separate from SDK component/client resource lifetimes."""

from collections.abc import Awaitable, Callable

import anyio


class RetryableScopeError(RuntimeError):
    """A scope hook explicitly permits another attempt with the same scope ID.

    Hooks must be idempotent after partial effects and uncertain acknowledgements.
    Only this outer exception opts into retry; causes, groups and cancellation do
    not. Use ``raise RetryableScopeError(...) from error`` to retain diagnostics.
    """


async def retry_scope[ResultT](
    operation: Callable[[], Awaitable[ResultT]],
    *,
    after_attempt: Callable[[], Awaitable[None]] | None = None,
) -> ResultT:
    """Attempt a hook at most three times, with a fixed half-second pause.

    after_attempt saves exit progress even on failure/cancellation. Its errors
    never retry the hook. The caller validates returned enter State. Ordinary
    hook failures and cancellation propagate without another call.
    """
    for attempt in range(3):
        try:
            return await operation()
        except RetryableScopeError:
            if attempt == 2:
                raise
        finally:
            if after_attempt is not None:
                await after_attempt()
        await anyio.sleep(0.5)
    raise AssertionError("Unreachable scope retry")
