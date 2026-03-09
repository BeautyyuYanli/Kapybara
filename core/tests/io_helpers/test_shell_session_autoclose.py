import pytest

from k.io_helpers.shell import ShellSession, ShellSessionOptions


@pytest.mark.anyio
async def test_shell_session_next_autocloses_on_exit() -> None:
    session = ShellSession(
        'python -c \'import sys; print("OUT"); print("ERR", file=sys.stderr); sys.exit(7)\''
    )

    stdout, stderr, code = await session.next()

    assert code == 7
    assert stdout == b"OUT\n"
    assert stderr == b"ERR\n"
    assert session.is_closed() is True

    # Interrupt clears the underlying process handles.
    assert session._process is None

    with pytest.raises(RuntimeError, match="Session is closed"):
        await session.next()


@pytest.mark.anyio
async def test_shell_session_next_does_not_autoclose_on_timeout() -> None:
    session = ShellSession(
        "python -c 'import time; print(\"START\"); time.sleep(2)'",
        options=ShellSessionOptions(
            timeout_seconds=0.05,
            terminate_wait_seconds=0.1,
            kill_wait_seconds=0.1,
            process_close_wait_seconds=0.1,
        ),
    )
    try:
        _stdout, _stderr, code = await session.next()
        assert code is None
        assert session.is_closed() is False
    finally:
        await session.interrupt()
        assert session.is_closed() is True


@pytest.mark.anyio
async def test_shell_session_next_allows_per_call_timeout_override() -> None:
    session = ShellSession(
        "python -c 'import time; print(\"START\"); time.sleep(0.05)'",
        options=ShellSessionOptions(
            timeout_seconds=0.01,
            terminate_wait_seconds=0.1,
            kill_wait_seconds=0.1,
            process_close_wait_seconds=0.1,
        ),
    )

    stdout, _stderr, code = await session.next(timeout_seconds=0.2)

    assert b"START\n" in stdout
    assert code == 0
    assert session.is_closed() is True


@pytest.mark.anyio
async def test_shell_session_interrupt_from_different_task_does_not_crash(
    anyio_backend,
) -> None:
    """
    Regression test: ShellSession must not rely on anyio.TaskGroup across tool calls.

    Tool calls (e.g., start vs. end) can execute in different tasks; cleanup must
    not raise cancel-scope errors when called from a different task than startup.
    """

    import anyio

    if anyio_backend != "asyncio":
        pytest.skip("ShellSession currently requires the asyncio anyio backend.")

    session = ShellSession(
        "python -c 'import time; print(\"READY\"); time.sleep(10)'",
        options=ShellSessionOptions(
            timeout_seconds=0.05,
            terminate_wait_seconds=0.1,
            kill_wait_seconds=0.1,
            process_close_wait_seconds=0.1,
        ),
    )

    started = anyio.Event()

    async def starter() -> None:
        await session.ensure_started()
        started.set()
        # Keep this task alive briefly so startup and shutdown are definitely
        # performed by different tasks.
        await anyio.sleep(0.05)

    async def stopper() -> None:
        await started.wait()
        await session.interrupt()

    async with anyio.create_task_group() as tg:
        tg.start_soon(starter)
        tg.start_soon(stopper)

    assert session.is_closed() is True
