"""A scope hook's explicit promise that repeating this call is safe."""


class RetryableScopeError(RuntimeError):
    """Permit the host to repeat a hook with the same scope identity and Config.

    The host owns retry limits, storage and scheduling. Raising this outer error
    promises safe repetition after partial effects or uncertain acknowledgements;
    nested causes and exception groups make no such promise. Enter need not be
    retryable otherwise. Exit must be idempotent for explicit close or recovery,
    including calls with None State. Use exception chaining to retain diagnostics.
    """
