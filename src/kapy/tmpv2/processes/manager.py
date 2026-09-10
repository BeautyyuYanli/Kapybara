"""Independent Linux process ownership, durable observations, and context-managed cleanup.

OS pipes are owned directly so leader exit is observable independently of inherited
output descriptors. Only explicit termination, shutdown, or failure bounds output
collection; ordinary leader exit never kills descendants to manufacture EOF.
"""

import asyncio
import errno
import fcntl
import logging
import math
import os
import signal
import sys
import termios
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from platformdirs import user_state_path
from sqlalchemy import URL, event
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ._io import run_io
from .gc import gc_processes
from .models import ProcessRow
from .repository import TERMINAL, ProcessRepository, status
from .types import (
    OutputPage,
    OutputStream,
    ProcessError,
    ProcessPage,
    ProcessSpec,
    ProcessStatus,
    TerminalSize,
)

logger = logging.getLogger(__name__)
_CHUNK = 65_536


def _environment(env: dict[str, str]) -> dict[str, str]:
    if any(not key or "=" in key or "\0" in key or "\0" in value for key, value in env.items()):
        raise ProcessError("invalid_argument", "Invalid environment variable")
    return dict(env)


def _size(size: TerminalSize) -> None:
    if not 1 <= size.rows <= 1000 or not 1 <= size.columns <= 1000:
        raise ProcessError("invalid_argument", "Terminal dimensions must be between 1 and 1000")


def _signal(child: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        pass


async def _ready(fd: int) -> None:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()

    def readable() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_reader(fd, readable)
    try:
        await ready
    finally:
        loop.remove_reader(fd)


async def _read(fd: int, *, pty: bool = False) -> bytes:
    while True:
        try:
            return os.read(fd, _CHUNK)
        except BlockingIOError:
            await _ready(fd)
        except OSError as exc:
            if pty and exc.errno == errno.EIO:
                return b""
            raise


@dataclass(slots=True)
class _Execution:
    process_id: UUID
    child: asyncio.subprocess.Process | None = None
    master: int = -1
    readers: dict[str, int] = field(default_factory=dict)
    spools: dict[str, int] = field(default_factory=dict)
    finished_at: datetime | None = None
    spawned: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None
    drains: list[asyncio.Task[None]] = field(default_factory=list)


class ProcessManager:
    """Use open_process_manager to acquire this service and its lifetime.

    Accepted starts and termination belong to the manager, not their observers.
    Active OS handles disappear after execution and output finish; historical
    state is read from the repository. No OS process identity survives a restart.
    """

    def __init__(
        self, repository: ProcessRepository, output_dir: Path, child_env: dict[str, str]
    ) -> None:
        self._repository = repository
        self._output_dir = output_dir
        self._child_env = child_env
        self._entries: dict[UUID, _Execution] = {}
        self._admission = asyncio.Lock()
        self._accepted: set[asyncio.Task[Any]] = set()
        self._observers: set[asyncio.Task[Any]] = set()
        self._closing = False

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        if self._closing:
            raise ProcessError("closed", "Process manager is closed")
        task = asyncio.current_task()
        assert task is not None
        self._observers.add(task)
        try:
            yield
        except OSError, SQLAlchemyError:
            raise ProcessError("io_error", "Process operation failed") from None
        finally:
            self._observers.discard(task)

    def _accept[T](self, operation: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(operation)
        self._accepted.add(task)

        def finished(task: asyncio.Task[T]) -> None:
            self._accepted.discard(task)
            if not task.cancelled():
                task.exception()  # A disconnected observer may no longer retrieve the result.

        task.add_done_callback(finished)
        return task

    async def start(self, process_id: UUID, spec: ProcessSpec) -> ProcessStatus:
        """Accept a new ID. Existing IDs always conflict, including deleting records."""
        async with self._operation():
            if (
                not spec.argv
                or not spec.argv[0]
                or any("\0" in arg for arg in spec.argv)
                or "\0" in spec.cwd
                or not Path(spec.cwd).is_absolute()
            ):
                raise ProcessError("invalid_argument", "argv and absolute cwd are required")
            if spec.mode not in ("stdio", "pty"):
                raise ProcessError("invalid_argument", "Unknown process mode")
            if spec.mode == "stdio" and spec.terminal_size is not None:
                raise ProcessError("invalid_argument", "stdio has no terminal size")
            size = spec.terminal_size or TerminalSize()
            _size(size)
            copied = ProcessSpec(
                argv=tuple(spec.argv),
                cwd=spec.cwd,
                env={**self._child_env, **_environment(spec.env)},
                mode=spec.mode,
                terminal_size=size if spec.mode == "pty" else None,
            )
            return await asyncio.shield(self._accept(self._start(process_id, copied)))

    async def _start(self, process_id: UUID, spec: ProcessSpec) -> ProcessStatus:
        async with self._admission:
            if self._closing:
                raise ProcessError("closed", "Process manager is closing")
            if not await run_io(Path(spec.cwd).is_dir):
                raise ProcessError("invalid_argument", "cwd must be an existing directory")
            row = ProcessRow(
                process_id=process_id,
                mode=spec.mode,
                cwd=spec.cwd,
                state="starting",
                created_at=datetime.now(UTC),
            )
            if process_id in self._entries:
                raise ProcessError("conflict", "Process ID already exists")
            entry = _Execution(process_id)
            self._entries[process_id] = entry
            try:
                await self._repository.add(row)
            except BaseException as exc:
                self._entries.pop(process_id, None)
                entry.done.set()
                if isinstance(exc, IntegrityError):
                    raise ProcessError("conflict", "Process ID already exists") from None
                raise
            entry.task = asyncio.create_task(self._run(entry, spec), name=f"process-{process_id}")
            return status(row)

    async def get(self, process_id: UUID) -> ProcessStatus:
        async with self._operation():
            return status(await self._repository.get(process_id))

    async def list(self, *, after: UUID | None = None, limit: int = 50) -> ProcessPage:
        async with self._operation():
            if not 1 <= limit <= 200:
                raise ProcessError("invalid_argument", "limit must be between 1 and 200")
            return await self._repository.list(after, limit)

    async def wait(
        self,
        process_id: UUID,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> ProcessStatus:
        """Observe execution and output completion; canceling this call never sends a signal."""
        async with self._operation():
            if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
                raise ProcessError("invalid_argument", "timeout must be finite and nonnegative")
            entry = self._entries.get(process_id)
            row = await self._repository.get(process_id)
            if entry is not None and timeout != 0:
                try:
                    async with asyncio.timeout(timeout):
                        await entry.done.wait()
                except TimeoutError:
                    pass
                row = await self._repository.get(process_id)
            return status(row)

    async def read_output(
        self, process_id: UUID, *, stream: OutputStream, offset: int = 0, max_bytes: int = _CHUNK
    ) -> OutputPage:
        """Read a byte page; use next_offset to continue from its end.

        available_end is the file end observed by this read. An empty page alone
        does not indicate completion: eof requires collection to have ended and
        the page to reach that end. Consult output_state for completeness; eof
        also applies to incomplete output. An offset beyond available_end raises
        invalid_argument.
        """
        async with self._operation():
            if offset < 0 or not 1 <= max_bytes <= _CHUNK:
                raise ProcessError("invalid_argument", "Invalid output cursor or page size")
            row = await self._repository.get(process_id)
            self._retained(row)
            if stream not in (("stdout", "stderr") if row.mode == "stdio" else ("pty",)):
                raise ProcessError("invalid_argument", "Stream does not match process mode")

            def read() -> tuple[bytes, int]:
                try:
                    with (self._output_dir / str(process_id) / stream).open("rb") as output:
                        end = os.fstat(output.fileno()).st_size
                        output.seek(offset)
                        return output.read(min(max_bytes, max(0, end - offset))), end
                except FileNotFoundError:
                    if row.output_state == "collecting" or row.state in ("failed", "lost"):
                        return b"", 0
                    raise

            data, end = await run_io(read)
            if offset > end:
                raise ProcessError("invalid_argument", "Output cursor is beyond available data")
            return OutputPage(
                stream=stream,
                data=data,
                next_offset=offset + len(data),
                available_end=end,
                eof=row.output_state != "collecting" and offset + len(data) >= end,
            )

    @staticmethod
    def _retained(row: ProcessRow) -> None:
        if row.resource_state == "deleting":
            raise ProcessError("gone", "Process resources are being deleted")

    async def _pty(self, process_id: UUID) -> _Execution:
        row = await self._repository.get(process_id)
        self._retained(row)
        if row.mode != "pty":
            raise ProcessError("invalid_argument", "Operation requires a PTY")
        entry = self._entries.get(process_id)
        if entry is None or entry.finished_at is not None or entry.master < 0:
            raise ProcessError("conflict", "PTY is not running")
        return entry

    async def write(self, process_id: UUID, data: bytes) -> int:
        """One nonblocking write; zero or a short count leaves the remainder with the caller."""
        async with self._operation():
            if len(data) > _CHUNK:
                raise ProcessError("invalid_argument", "Input exceeds 65536 bytes")
            entry = await self._pty(process_id)
            try:
                return os.write(entry.master, data)
            except BlockingIOError:
                return 0

    async def resize(self, process_id: UUID, size: TerminalSize) -> ProcessStatus:
        async with self._operation():
            _size(size)
            entry = await self._pty(process_id)
            termios.tcsetwinsize(entry.master, (size.rows, size.columns))
            return status(await self._repository.get(process_id))

    async def terminate(self, process_id: UUID) -> ProcessStatus:
        async with self._operation():
            return await asyncio.shield(self._accept(self._terminate(process_id)))

    async def _terminate(self, process_id: UUID) -> ProcessStatus:
        row = await self._repository.get(process_id)
        self._retained(row)
        entry = self._entries.get(process_id)
        if entry is not None and not entry.done.is_set():
            self._request_stop(entry)
            await self._repository.mark_terminating(process_id)
        return status(await self._repository.get(process_id))

    async def release(self, process_id: UUID) -> ProcessStatus | None:
        """Commit deletion intent only; physical cleanup is performed by gc_processes."""
        async with self._operation():
            try:
                row = await self._repository.get(process_id)
            except ProcessError as exc:
                if exc.code == "not_found":
                    return None
                raise
            if row.resource_state == "deleting":
                return status(row)
            if row.state not in TERMINAL or row.output_state == "collecting":
                raise ProcessError("conflict", "Process execution and output must finish first")
            await self._repository.change(process_id, resource_state="deleting")
            return status(await self._repository.get(process_id))

    async def _spawn(self, entry: _Execution, spec: ProcessSpec) -> None:
        child_fds: list[int] = []
        error_read = -1
        try:
            root = self._output_dir / str(entry.process_id)

            def spools() -> None:
                root.mkdir(mode=0o700)
                streams = ("stdout", "stderr") if spec.mode == "stdio" else ("pty",)
                for stream in streams:
                    entry.spools[stream] = os.open(
                        root / stream,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
                        0o600,
                    )

            await run_io(spools)
            if spec.mode == "stdio":
                for stream in ("stdout", "stderr"):
                    reader, writer = os.pipe()
                    os.set_blocking(reader, False)
                    entry.readers[stream] = reader
                    child_fds.append(writer)
                entry.child = await asyncio.create_subprocess_exec(
                    *spec.argv,
                    cwd=spec.cwd,
                    env=spec.env,
                    start_new_session=True,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=child_fds[0],
                    stderr=child_fds[1],
                )
            else:
                entry.master, slave = os.openpty()
                child_fds.append(slave)
                os.set_blocking(entry.master, False)
                size = spec.terminal_size or TerminalSize()
                termios.tcsetwinsize(slave, (size.rows, size.columns))
                error_read, error_write = os.pipe()
                child_fds.append(error_write)
                os.set_blocking(error_read, False)
                entry.child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(Path(__file__).with_name("_pty_exec.py")),
                    str(error_write),
                    *spec.argv,
                    cwd=spec.cwd,
                    env=spec.env,
                    start_new_session=True,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    pass_fds=(error_write,),
                )
                os.close(error_write)
                child_fds.remove(error_write)
                if await _read(error_read):
                    raise OSError("PTY child setup failed")
        finally:
            for fd in child_fds:
                os.close(fd)
            if error_read >= 0:
                os.close(error_read)

    async def _drain(self, entry: _Execution, stream: str) -> None:
        fd = entry.master if stream == "pty" else entry.readers[stream]
        while data := await _read(fd, pty=stream == "pty"):

            def append(data: bytes = data) -> None:
                remaining = memoryview(data)
                while remaining:
                    remaining = remaining[os.write(entry.spools[stream], remaining) :]

            await run_io(append)

    async def _leader(self, entry: _Execution) -> None:
        assert entry.child is not None
        code = await entry.child.wait()
        # This records the OS observation, not whether its database write succeeded.
        entry.finished_at = datetime.now(UTC)
        await self._repository.change(
            entry.process_id, state="exited", exit_code=code, finished_at=entry.finished_at
        )

    async def _run(self, entry: _Execution, spec: ProcessSpec) -> None:
        leader: asyncio.Task[None] | None = None
        complete = False
        failed_execution = False
        stage = "spawn"
        try:
            await self._spawn(entry, spec)
            stage = "record_running"
            await self._repository.change(
                entry.process_id,
                state="terminating" if entry.stop_task is not None else "running",
            )
            leader = asyncio.create_task(self._leader(entry))
            streams = ("pty",) if spec.mode == "pty" else ("stdout", "stderr")
            entry.drains = [asyncio.create_task(self._drain(entry, stream)) for stream in streams]
            entry.spawned.set()
            stage = "execution_and_output"
            await asyncio.gather(leader, *entry.drains)
            stage = "sync_output"
            for fd in entry.spools.values():
                await run_io(lambda fd=fd: os.fsync(fd))
            complete = True
        except (Exception, asyncio.CancelledError) as exc:
            # Killing a still-running child because management failed must not turn
            # that failure into a successful observation of a signal-induced exit.
            failed_execution = (
                stage == "spawn" or entry.child is None or entry.child.returncode is None
            )
            logger.warning(
                "Process failed: %s stage=%s exception=%s errno=%s",
                entry.process_id,
                stage,
                type(exc).__name__,
                exc.errno if isinstance(exc, OSError) else None,
            )
            for task in entry.drains:
                task.cancel()
            await asyncio.gather(*entry.drains, return_exceptions=True)
            if entry.child is not None:
                _signal(entry.child, signal.SIGKILL)
                await entry.child.wait()
            if leader is not None:
                await asyncio.gather(leader, return_exceptions=True)
        finally:
            entry.spawned.set()
            for fd in (*entry.readers.values(), *entry.spools.values()):
                os.close(fd)
            if entry.master >= 0:
                os.close(entry.master)
                entry.master = -1
            try:
                # Finalization also supplies the terminal execution fields in case
                # the earlier leader-exit write failed while output was collecting.
                await self._repository.change(
                    entry.process_id,
                    state="failed" if failed_execution else "exited",
                    finished_at=entry.finished_at or datetime.now(UTC),
                    exit_code=entry.child.returncode if entry.child else None,
                    output_state="complete" if complete else "incomplete",
                )
            except (OSError, SQLAlchemyError) as exc:
                logger.error(
                    "Process failed: %s stage=record_completion exception=%s errno=%s",
                    entry.process_id,
                    type(exc).__name__,
                    exc.errno if isinstance(exc, OSError) else None,
                )
            finally:
                entry.done.set()
                self._entries.pop(entry.process_id, None)

    def _request_stop(self, entry: _Execution) -> None:
        if entry.stop_task is None:
            entry.stop_task = self._accept(self._stop(entry))

    async def _stop(self, entry: _Execution) -> None:
        await entry.spawned.wait()
        if entry.done.is_set() or entry.child is None:
            return
        _signal(entry.child, signal.SIGTERM)
        try:
            async with asyncio.timeout(2):
                await entry.done.wait()
            return
        except TimeoutError:
            _signal(entry.child, signal.SIGKILL)
        try:
            async with asyncio.timeout(2):
                await entry.done.wait()
        except TimeoutError:
            # Escaped descendants may retain pipes after the group is gone.
            for task in entry.drains:
                task.cancel()
            await entry.done.wait()

    async def _close(self) -> None:
        self._closing = True
        if self._accepted:
            await asyncio.gather(*self._accepted, return_exceptions=True)
        entries = tuple(self._entries.values())
        for entry in entries:
            self._request_stop(entry)
        await asyncio.gather(*(entry.task for entry in entries if entry.task is not None))
        if self._accepted:
            await asyncio.gather(*self._accepted, return_exceptions=True)
        # Finish borrowed database/file operations before the engine and lock close.
        observers = tuple(self._observers)
        for task in observers:
            task.cancel()
        await asyncio.gather(*observers, return_exceptions=True)


def _prepare(state_dir: Path) -> int:
    missing: list[Path] = []
    current = state_dir
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    info = state_dir.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProcessError(
            "invalid_argument", "State directory must be private and owned by this user"
        )
    lock = os.open(state_dir / "processes.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProcessError("conflict", "Process state directory is already in use") from None
        database = os.open(
            state_dir / "processes.sqlite3", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600
        )
        os.close(database)
        (state_dir / "processes").mkdir(mode=0o700, exist_ok=True)
        return lock
    except BaseException:
        os.close(lock)
        raise


def _configure(connection: Any, _: Any) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
    finally:
        cursor.close()


async def _sweep(repository: ProcessRepository, output_dir: Path) -> None:
    try:
        await gc_processes(repository, output_dir)
    except Exception:
        logger.warning("Process resource sweep failed")


async def _periodic_gc(repository: ProcessRepository, output_dir: Path) -> None:
    while True:
        await asyncio.sleep(5)
        await _sweep(repository, output_dir)


@asynccontextmanager
async def open_process_manager(
    *,
    state_dir: Path | None = None,
    child_env: dict[str, str] | None = None,
) -> AsyncIterator[ProcessManager]:
    """Own the state lock, database, accepted executions and GC until context exit."""
    root = state_dir if state_dir is not None else user_state_path("kapy")
    if not root.is_absolute():
        raise ProcessError("invalid_argument", "state_dir must be absolute")
    environment = _environment(child_env or {})
    lock = -1
    engine: AsyncEngine | None = None
    manager: ProcessManager | None = None
    gc_task: asyncio.Task[None] | None = None
    try:

        def prepare() -> None:
            nonlocal lock
            lock = _prepare(root)

        try:
            await run_io(prepare)
            engine = create_async_engine(
                URL.create("sqlite+aiosqlite", database=str(root / "processes.sqlite3"))
            )
            event.listen(engine.sync_engine, "connect", _configure)
            async with engine.begin() as connection:
                await connection.run_sync(ProcessRow.metadata.create_all)
            repository = ProcessRepository(engine)
            await repository.recover()
            await _sweep(repository, root / "processes")
            manager = ProcessManager(repository, root / "processes", environment)
            gc_task = asyncio.create_task(_periodic_gc(repository, root / "processes"))
        except OSError, SQLAlchemyError:
            raise ProcessError("io_error", "Process manager storage failed") from None
        yield manager
    finally:

        async def close() -> None:
            if manager is not None:
                manager._closing = True
            try:
                if gc_task is not None:
                    gc_task.cancel()
                    await asyncio.gather(gc_task, return_exceptions=True)
                if manager is not None:
                    await manager._close()
            finally:
                try:
                    if engine is not None:
                        await engine.dispose()
                finally:
                    if lock >= 0:
                        os.close(lock)

        cleanup = asyncio.create_task(close())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
