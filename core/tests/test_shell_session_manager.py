import anyio
import pytest

from k.io_helpers.shell import ShellSessionManager, ShellSessionOptions


@pytest.mark.anyio
async def test_shell_session_manager_new_shell_registers_and_next_prunes() -> None:
    manager = ShellSessionManager()
    session_id = await manager.new_shell(
        'python -c \'import sys; print("OUT"); print("ERR", file=sys.stderr)\''
    )

    sessions = await manager.list_sessions()
    assert [s.session_id for s in sessions] == [session_id]

    stdout, stderr, code = await manager.next(session_id)
    assert code == 0
    assert stdout == b"OUT\n"
    assert stderr == b"ERR\n"

    assert await manager.list_sessions() == []


@pytest.mark.anyio
async def test_shell_session_manager_list_sessions_prunes_exited_sessions() -> None:
    manager = ShellSessionManager()
    _session_id = await manager.new_shell("python -c 'print(\"X\")'")

    # The process exits quickly, but we intentionally do not call next().
    await anyio.sleep(0.05)
    assert await manager.list_sessions() == []


@pytest.mark.anyio
async def test_shell_session_manager_interrupt_unregisters() -> None:
    manager = ShellSessionManager()
    session_id = await manager.new_shell(
        "python -c 'import time; print(\"READY\"); time.sleep(10)'",
        options=ShellSessionOptions(
            timeout_seconds=0.05,
            terminate_wait_seconds=0.1,
            kill_wait_seconds=0.1,
            process_close_wait_seconds=0.1,
        ),
    )

    await manager.interrupt(session_id)
    assert await manager.list_sessions() == []


@pytest.mark.anyio
async def test_shell_session_manager_next_allows_timeout_override() -> None:
    manager = ShellSessionManager()
    session_id = await manager.new_shell(
        "python -c 'import time; print(\"READY\"); time.sleep(0.05)'",
        options=ShellSessionOptions(timeout_seconds=0.01),
    )

    stdout, _stderr, code = await manager.next(session_id, timeout_seconds=0.2)
    assert b"READY\n" in stdout
    assert code == 0
    assert await manager.list_sessions() == []


@pytest.mark.anyio
async def test_shell_session_manager_close_guards_operations() -> None:
    manager = ShellSessionManager()
    await manager.close()

    with pytest.raises(RuntimeError, match="ShellSessionManager is closed"):
        await manager.list_sessions()
