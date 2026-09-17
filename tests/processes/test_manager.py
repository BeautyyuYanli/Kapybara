"""Real processes, PTYs and SQLite: run only in the disposable execution container."""

import asyncio
import os
import signal
import sqlite3
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from kapy.processes import (
    ProcessError,
    ProcessManager,
    ProcessSpec,
    TerminalSize,
    open_process_manager,
)
from kapy.processes.gc import gc_processes
from kapy.processes.models import ProcessRow
from kapy.processes.repository import ProcessRepository

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not Path("/.dockerenv").exists(),
        reason="Real process tests require the dedicated Docker machine",
    ),
]


@pytest_asyncio.fixture
async def manager(tmp_path: Path) -> AsyncIterator[ProcessManager]:
    async with open_process_manager(state_dir=tmp_path / "state") as manager:
        yield manager


def command(tmp_path: Path, code: str, *, pty: bool = False) -> ProcessSpec:
    return ProcessSpec(
        argv=(sys.executable, "-c", code), cwd=str(tmp_path), mode="pty" if pty else "stdio"
    )


async def output_contains(
    manager: ProcessManager, pid: UUID, expected: bytes, *, pty: bool = False
):
    async with asyncio.timeout(5):
        while True:
            page = await manager.read_output(pid, stream="pty" if pty else "stdout")
            if expected in page.data:
                return page
            await asyncio.sleep(0.01)


async def test_stdio_pages_environment_and_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PROCESS_SECRET", "must-not-inherit")
    pid = uuid4()
    async with open_process_manager(
        state_dir=tmp_path / "state", child_env={"BASE": "base"}
    ) as mgr:
        accepted = await mgr.start(
            pid,
            ProcessSpec(
                argv=(
                    sys.executable,
                    "-c",
                    "import os; assert os.read(0,1)==b''; "
                    "assert 'PROCESS_SECRET' not in os.environ; "
                    "assert os.environ['BASE']=='override'; "
                    "[os.write(1,bytes((i,))*16384) for i in range(256)]; "
                    "os.write(2,b'error'); exit(7)",
                ),
                cwd=str(tmp_path),
                env={"BASE": "override"},
            ),
        )
        assert accepted.state == "starting"
        result = await mgr.wait(pid, timeout=10)
        assert (result.state, result.output_state, result.exit_code) == ("exited", "complete", 7)
        assert result.created_at.tzinfo is UTC
        position = 0
        collected = bytearray()
        while True:
            page = await mgr.read_output(pid, stream="stdout", offset=position, max_bytes=16384)
            assert len(page.data) <= 16384
            assert page.next_offset == position + len(page.data)
            collected.extend(page.data)
            position = page.next_offset
            if page.eof:
                break
        assert position == 4 * 1024 * 1024
        assert collected == b"".join(bytes((i,)) * 16384 for i in range(256))
        assert (await mgr.read_output(pid, stream="stderr")).data == b"error"
        with pytest.raises(ProcessError) as caught:
            await mgr.read_output(pid, stream="stdout", offset=position + 1)
        assert caught.value.code == "invalid_argument"
    async with open_process_manager(state_dir=tmp_path / "state") as mgr:
        assert (await mgr.get(pid)).exit_code == 7
        assert (await mgr.read_output(pid, stream="stdout", offset=position)).eof
    with sqlite3.connect(tmp_path / "state/processes.sqlite3") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(processes)")}
        assert not {"argv", "env", "pid", "pgid"} & columns
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)


async def test_leader_exit_keeps_descendant_output(manager: ProcessManager, tmp_path: Path):
    pid = uuid4()
    await manager.start(
        pid,
        command(
            tmp_path,
            "import os,time; p=os.fork(); os._exit(0) if p else None; "
            "print('first',flush=True); time.sleep(1); print('last',flush=True)",
        ),
    )
    await output_contains(manager, pid, b"first")
    interim = await manager.wait(pid, timeout=0.05)
    assert interim.state == "exited"
    assert interim.output_state == "collecting"
    result = await manager.wait(pid, timeout=5)
    assert result.output_state == "complete"
    assert (await manager.read_output(pid, stream="stdout")).data == b"first\nlast\n"


async def test_cancel_start_observer_after_commit_keeps_execution(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    committed = asyncio.Event()
    resume = asyncio.Event()
    add = ProcessRepository.add

    async def pause_after_commit(self: ProcessRepository, row: ProcessRow) -> None:
        await add(self, row)
        committed.set()
        await resume.wait()

    monkeypatch.setattr(ProcessRepository, "add", pause_after_commit)
    pid = uuid4()
    observer = asyncio.create_task(manager.start(pid, command(tmp_path, "print('accepted')")))
    await committed.wait()
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    resume.set()
    result = await manager.wait(pid, timeout=5)
    assert result.output_state == "complete"
    assert (await manager.read_output(pid, stream="stdout")).data == b"accepted\n"


async def test_wait_cancellation_and_duplicate_ids(manager: ProcessManager, tmp_path: Path):
    pid = uuid4()
    spec = command(
        tmp_path, "import time; print('ready',flush=True); time.sleep(.4); print('done')"
    )
    await manager.start(pid, spec)
    await output_contains(manager, pid, b"ready")
    waiting = asyncio.create_task(manager.wait(pid))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    with pytest.raises(ProcessError) as caught:
        await manager.start(pid, spec)
    assert caught.value.code == "conflict"
    assert (await manager.wait(pid, timeout=5)).exit_code == 0
    with pytest.raises(ProcessError) as caught:
        await manager.start(pid, command(tmp_path, "print('different')"))
    assert caught.value.code == "conflict"


async def test_full_pty_file_input_resize_and_restart(tmp_path: Path):
    pid = uuid4()
    async with open_process_manager(state_dir=tmp_path / "state") as mgr:
        await mgr.start(
            pid,
            command(
                tmp_path,
                "import os,fcntl,termios,struct; assert os.isatty(0); "
                "os.close(os.open('/dev/tty',os.O_RDWR)); "
                "os.write(1,b'x'*20000+b'READY\\n'); line=input(); "
                "size=struct.unpack('HHHH',fcntl.ioctl(0,termios.TIOCGWINSZ,b'\\0'*8)); "
                "print('RESULT',line,size[0],size[1])",
                pty=True,
            ),
        )
        await output_contains(mgr, pid, b"READY", pty=True)
        await mgr.resize(pid, TerminalSize(rows=31, columns=92))
        assert await mgr.write(pid, b"hello\n") == 6
        result = await mgr.wait(pid, timeout=5)
        assert result.output_state == "complete"
        page = await mgr.read_output(pid, stream="pty")
        assert page.data.startswith(b"x" * 20000)
        assert b"RESULT hello 31 92" in page.data
        assert page.eof
        with pytest.raises(ProcessError) as caught:
            await mgr.write(pid, b"late")
        assert caught.value.code == "conflict"
    async with open_process_manager(state_dir=tmp_path / "state") as mgr:
        assert (await mgr.read_output(pid, stream="pty")).data == page.data
        assert (tmp_path / "state/processes" / str(pid) / "pty").read_bytes() == page.data


@pytest.mark.parametrize("pty", [False, True])
async def test_spawn_failure_is_safe_and_releasable(
    manager: ProcessManager, tmp_path: Path, pty: bool
):
    pid = uuid4()
    await manager.start(
        pid,
        ProcessSpec(
            argv=("/does-not-exist-private-command",),
            cwd=str(tmp_path),
            mode="pty" if pty else "stdio",
        ),
    )
    result = await manager.wait(pid, timeout=5)
    assert result.state == "failed"
    assert result.output_state == "incomplete"
    assert await manager.release(pid) is not None


async def test_sigterm_escalation_and_release_conflict(manager: ProcessManager, tmp_path: Path):
    pid = uuid4()
    await manager.start(
        pid,
        command(
            tmp_path,
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(60)",
        ),
    )
    await output_contains(manager, pid, b"ready")
    with pytest.raises(ProcessError) as caught:
        await manager.release(pid)
    assert caught.value.code == "conflict"
    began = asyncio.get_running_loop().time()
    await manager.terminate(pid)
    await manager.terminate(pid)
    result = await manager.wait(pid, timeout=6)
    assert 1.8 <= asyncio.get_running_loop().time() - began < 6
    assert result.exit_code == -signal.SIGKILL
    assert result.output_state == "complete"


async def test_escaped_output_has_bounded_incomplete_cleanup(
    manager: ProcessManager, tmp_path: Path
):
    pid = uuid4()
    await manager.start(
        pid,
        command(
            tmp_path,
            "import os,time; p=os.fork(); os._exit(0) if p else None; "
            "os.setsid(); print(os.getpid(),flush=True); time.sleep(60)",
        ),
    )
    page = await output_contains(manager, pid, b"\n")
    escaped = int(page.data)
    try:
        await manager.terminate(pid)
        result = await manager.wait(pid, timeout=6)
        assert (result.state, result.exit_code, result.output_state) == ("exited", 0, "incomplete")
    finally:
        os.kill(escaped, signal.SIGKILL)


async def test_gc_failures_keep_deleting_rows_then_allow_id_reuse(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pid = uuid4()
    await manager.start(pid, command(tmp_path, "print('retained')"))
    await manager.wait(pid, timeout=5)
    output = tmp_path / "state/processes" / str(pid) / "stdout"
    deleting = await manager.release(pid)
    assert deleting is not None and deleting.resource_state == "deleting"
    assert output.exists()
    assert (await manager.release(pid)) == deleting
    with pytest.raises(ProcessError) as caught:
        await manager.start(pid, command(tmp_path, "print('new')"))
    assert caught.value.code == "conflict"
    with pytest.raises(ProcessError) as caught:
        await manager.read_output(pid, stream="stdout")
    assert caught.value.code == "gone"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'state/processes.sqlite3'}")
    repository = ProcessRepository(engine)
    try:
        with monkeypatch.context() as patch:

            def fail_remove(*args: object, **kwargs: object) -> None:
                raise PermissionError

            patch.setattr("kapy.processes.gc.shutil.rmtree", fail_remove)
            assert await gc_processes(repository, output.parent.parent) == 0
            assert output.exists()
        with monkeypatch.context() as patch:

            async def fail_delete(self: ProcessRepository, process_id: UUID) -> int:
                raise OperationalError("", {}, Exception())

            patch.setattr(ProcessRepository, "delete", fail_delete)
            assert await gc_processes(repository, output.parent.parent) == 0
            assert not output.exists()
            assert (await manager.get(pid)).resource_state == "deleting"
        assert await gc_processes(repository, output.parent.parent) == 1
        assert await gc_processes(repository, output.parent.parent) == 0
        assert await manager.release(pid) is None
        await manager.start(pid, command(tmp_path, "print('new')"))
        assert (await manager.wait(pid, timeout=5)).exit_code == 0
        assert await asyncio.to_thread(tmp_path.is_dir)
    finally:
        await engine.dispose()


async def test_recovery_lost_and_startup_gc(tmp_path: Path):
    root = tmp_path / "state"
    async with open_process_manager(state_dir=root):
        pass
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'processes.sqlite3'}")
    repository = ProcessRepository(engine)
    interrupted, output_interrupted, deleting = uuid4(), uuid4(), uuid4()
    try:
        await repository.add(
            ProcessRow(
                process_id=interrupted,
                mode="stdio",
                cwd=str(tmp_path),
                state="running",
                created_at=datetime.now(UTC),
            )
        )
        for pid, resource_state, output_state in (
            (output_interrupted, "active", "collecting"),
            (deleting, "deleting", "complete"),
        ):
            await repository.add(
                ProcessRow(
                    process_id=pid,
                    mode="stdio",
                    cwd=str(tmp_path),
                    state="exited",
                    exit_code=3,
                    created_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    resource_state=resource_state,
                    output_state=output_state,
                )
            )
    finally:
        await engine.dispose()
    async with open_process_manager(state_dir=root) as mgr:
        result = await mgr.get(interrupted)
        assert (result.state, result.output_state) == ("lost", "incomplete")
        assert result.finished_at is not None and result.finished_at.tzinfo is UTC
        result = await mgr.get(output_interrupted)
        assert (result.state, result.exit_code, result.output_state) == ("exited", 3, "incomplete")
        with pytest.raises(ProcessError) as caught:
            await mgr.get(deleting)
        assert caught.value.code == "not_found"


async def test_xdg_lock_shutdown_and_closed_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    pid = uuid4()
    async with open_process_manager() as mgr:
        await mgr.start(
            pid, command(tmp_path, "import time; print('ready',flush=True); time.sleep(60)")
        )
        await output_contains(mgr, pid, b"ready")
        with pytest.raises(ProcessError) as caught:
            async with open_process_manager():
                pass
        assert caught.value.code == "conflict"
        db = tmp_path / "kapy/processes.sqlite3"
        assert db.stat().st_mode & 0o777 == 0o600
        assert db.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ProcessError) as caught:
        await mgr.get(pid)
    assert caught.value.code == "closed"
    async with open_process_manager() as reopened:
        result = await reopened.get(pid)
        assert result.state == "exited"
        assert result.resource_state == "active"
        assert result.output_state == "complete"


async def test_pagination_and_input_validation(manager: ProcessManager, tmp_path: Path):
    ids = [UUID(int=i) for i in (30, 10, 20)]
    for pid in ids:
        await manager.start(pid, command(tmp_path, "pass"))
    first = await manager.list(limit=2)
    assert [item.process_id for item in first.items] == [UUID(int=10), UUID(int=20)]
    assert first.next_after == UUID(int=20)
    second = await manager.list(after=first.next_after, limit=2)
    assert [item.process_id for item in second.items] == [UUID(int=30)]
    assert second.next_after is None
    with pytest.raises(ProcessError) as caught:
        await manager.start(uuid4(), ProcessSpec(argv=("true",), cwd="relative"))
    assert caught.value.code == "invalid_argument"
    for timeout in (-1, float("inf"), float("nan")):
        with pytest.raises(ProcessError):
            await manager.wait(ids[0], timeout=timeout)
    with pytest.raises(ProcessError):
        await manager.write(ids[0], b"x")


async def test_initialization_failure_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with monkeypatch.context() as patch:

        async def fail_recovery(self: ProcessRepository) -> None:
            raise OSError("private-storage-detail")

        patch.setattr(ProcessRepository, "recover", fail_recovery)
        with pytest.raises(ProcessError) as caught:
            async with open_process_manager(state_dir=tmp_path / "state"):
                pass
        assert caught.value.code == "io_error"
        assert "private-storage-detail" not in caught.value.message
    async with open_process_manager(state_dir=tmp_path / "state") as mgr:
        assert (await mgr.list()).items == ()


async def test_cancel_context_owner_joins_execution_before_unlock(tmp_path: Path):
    ready = asyncio.Event()
    pid = uuid4()

    async def owner() -> None:
        async with open_process_manager(state_dir=tmp_path / "state") as mgr:
            await mgr.start(
                pid, command(tmp_path, "import time; print('ready',flush=True); time.sleep(60)")
            )
            await output_contains(mgr, pid, b"ready")
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with open_process_manager(state_dir=tmp_path / "state") as mgr:
        result = await mgr.get(pid)
        assert (result.state, result.output_state, result.resource_state) == (
            "exited",
            "complete",
            "active",
        )
        assert result.exit_code == -signal.SIGTERM


async def test_context_preserves_callers_exception(tmp_path: Path):
    with pytest.raises(OSError, match="caller-owned error"):
        async with open_process_manager(state_dir=tmp_path / "state"):
            raise OSError("caller-owned error")
    async with open_process_manager(state_dir=tmp_path / "state"):
        pass


async def test_exit_commit_failure_still_finishes_and_releases(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    pid = uuid4()
    change = ProcessRepository.change
    interrupted = False

    async def interrupt_exit(self: ProcessRepository, process_id: UUID, **values: object) -> None:
        nonlocal interrupted
        if process_id == pid and values.get("state") == "exited" and not interrupted:
            interrupted = True
            raise OperationalError("private-query-text", {}, Exception("private-connection-detail"))
        await change(self, process_id, **values)

    monkeypatch.setattr(ProcessRepository, "change", interrupt_exit)
    await manager.start(pid, command(tmp_path, "exit(7)"))
    result = await manager.wait(pid, timeout=5)
    assert interrupted
    assert (result.state, result.exit_code, result.output_state) == ("exited", 7, "incomplete")
    assert result.finished_at is not None
    assert result.finished_at.tzinfo is UTC
    assert await manager.release(pid) is not None
    assert str(pid) in caplog.text and "OperationalError" in caplog.text
    assert "private-query-text" not in caplog.text
    assert "private-connection-detail" not in caplog.text


async def test_output_write_failure_of_running_child_is_failed(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    import errno
    import stat

    pid = uuid4()
    write = os.write
    interrupted = False

    def disk_full(fd: int, data: bytes | memoryview) -> int:
        nonlocal interrupted
        if not interrupted and stat.S_ISREG(os.fstat(fd).st_mode):
            interrupted = True
            raise OSError(errno.ENOSPC, "private-output-detail")
        return write(fd, data)

    monkeypatch.setattr(os, "write", disk_full)
    await manager.start(
        pid,
        command(
            tmp_path, "import time; print('private-command-marker',flush=True); time.sleep(60)"
        ),
    )
    result = await manager.wait(pid, timeout=5)
    assert interrupted
    assert (result.state, result.output_state) == ("failed", "incomplete")
    assert result.finished_at is not None
    assert await manager.release(pid) is not None
    assert str(pid) in caplog.text and "exception=OSError" in caplog.text
    assert f"errno={errno.ENOSPC}" in caplog.text
    assert "private-output-detail" not in caplog.text
    assert "private-command-marker" not in caplog.text
