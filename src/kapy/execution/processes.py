"""Durable process IDs, complete stdio spools and bounded interactive PTYs."""

import asyncio
import base64
import errno
import fcntl
import os
import shutil
import signal
import struct
import sys
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from kapy.rpc import JsonObject, JsonParams, JsonValue, RpcError

from ._common import (
    CHUNK_SIZE,
    byte_chunk,
    decode_chunk,
    error,
    fields,
    fingerprint,
    identifier,
    integer,
    invalid,
    session_identifier,
    string,
)
from .store import ExecutionStore, secure_directory

TERMINAL = {"exited", "killed", "failed", "lost", "released"}
IDENTITY_ENV = {"KAPY_MACHINE_ID", "KAPY_SESSION_ID", "KAPY_SESSION_TOKEN", "KAPY_DAEMON_SOCKET"}


def safe_env(value: JsonValue) -> dict[str, str]:
    if not isinstance(value, dict):
        raise invalid("env must be an object")
    result: dict[str, str] = {}
    for key, content in value.items():
        if (
            not key
            or "=" in key
            or "\0" in key
            or not isinstance(content, str)
            or "\0" in content
            or key in IDENTITY_ENV
            or any(
                word in key.upper()
                for word in (
                    "MACHINE_TOKEN",
                    "DATABASE_URL",
                    "OPENAI_API_KEY",
                    "TELEGRAM",
                    "CONTROL_TOKEN",
                )
            )
        ):
            raise invalid("env contains a reserved or invalid variable")
        result[key] = content
    return result


def _identity(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except OSError, IndexError:
        return None


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def _remove_root(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


@dataclass(slots=True)
class _Process:
    signature: str
    info: JsonObject
    root: Path
    meta: JsonObject = field(default_factory=dict)
    child: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None
    cleanup: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    read_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    master: int = -1
    fds: dict[str, int] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    tail: bytes = b""
    last_output: float = field(default_factory=time.monotonic)
    complete: bool = True


class ProcessManager:
    """The daemon owns this manager; RPC disconnection only cancels observers."""

    def __init__(self, store: ExecutionStore, *, child_env: dict[str, str] | None = None) -> None:
        self.store = store
        self.child_env = safe_env(cast(JsonValue, child_env or {}))
        self._entries: dict[tuple[str, str], _Process] = {}
        self._begin_lock = asyncio.Lock()
        self._operations: set[asyncio.Task[JsonValue]] = set()
        self._closing = False

    @property
    def active_count(self) -> int:
        return sum(not item.done.is_set() for item in self._entries.values())

    async def _save(self, entry: _Process) -> None:
        entry.meta["sizes"] = cast(JsonValue, dict(entry.sizes))
        if entry.info["mode"] == "pty":
            entry.meta["tail"] = base64.b64encode(entry.tail).decode("ascii")
        await self.store.save_process(entry.signature, dict(entry.info), dict(entry.meta))

    async def initialize(self) -> None:
        boot_id = await self.store.io.run(_boot_id)
        for record in await self.store.process_records():
            info = cast(JsonObject, record["info"])
            meta = cast(JsonObject, record["meta"])
            sid, pid = str(info["session_id"]), str(info["process_id"])
            root = self.store.paths.session_cwd(sid).parent / "processes" / pid
            entry = _Process(str(record["fingerprint"]), info, root, meta)
            entry.sizes = cast(dict[str, int], meta.get("sizes", {}))
            entry.tail = base64.b64decode(str(meta.get("tail", "")))
            if info["state"] not in TERMINAL:
                old_pid = meta.get("pid")
                if (
                    isinstance(old_pid, int)
                    and meta.get("identity") is not None
                    and boot_id is not None
                    and meta.get("boot_id") == boot_id
                ):
                    if await self.store.io.run(_identity, old_pid) == meta["identity"]:
                        await self.store.io.run(_kill_group, old_pid)
                info.update(
                    state="lost",
                    output_complete=False,
                    error={"kind": "io_error", "message": "Daemon interrupted process"},
                )
            if info["mode"] == "stdio" and info["state"] != "released":
                for name in ("stdout", "stderr"):
                    expected_size = entry.sizes.get(name, 0)
                    missing = False
                    try:
                        actual_size = (await self.store.io.run((root / name).stat)).st_size
                    except FileNotFoundError:
                        missing = True
                        actual_size = 0
                    if info["output_complete"] and (missing or actual_size != expected_size):
                        info["output_complete"] = False
                        info["error"] = {
                            "kind": "io_error",
                            "message": "Stored process output is missing or changed",
                        }
                    entry.sizes[name] = actual_size
            if info["state"] == "released":
                await self.store.io.run(_remove_root, root)
            entry.done.set()
            self._entries[sid, pid] = entry
            await self._save(entry)

    async def handle(self, method: str, params: JsonParams) -> JsonValue:
        if self._closing:
            raise error("offline", "Process manager is closing")
        if not isinstance(params, dict):
            raise invalid()
        if len(self._operations) >= 64:
            raise error("resource_limit", "Too many process operations")
        task = asyncio.create_task(self._dispatch(method, params), name="process-operation")
        self._operations.add(task)

        def finished(done: asyncio.Task[JsonValue]) -> None:
            self._operations.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _dispatch(self, method: str, params: JsonObject) -> JsonValue:
        if method == "process.start":
            return await self._start(params)
        sid = session_identifier(params.get("session_id"))
        if method == "process.list":
            fields(params, {"session_id"}, {"after", "limit"})
            after = identifier(params["after"], "after") if "after" in params else ""
            limit = integer(params.get("limit", 50), "limit", 1, 100)
            entries = sorted(
                (key[1], entry)
                for key, entry in self._entries.items()
                if key[0] == sid and key[1] > after
            )
            return {
                "items": [dict(entry.info) for _, entry in entries[:limit]],
                "next": entries[limit - 1][0] if len(entries) > limit else None,
            }
        optional = {
            "process.wait": {"cursor", "wait_ms", "max_bytes"},
            "process.kill": {"wait_ms"},
            "process.release": set(),
            "process.write": {"data_base64"},
            "process.resize": {"rows", "cols"},
        }
        if method not in optional:
            raise RpcError(-32601, "Method not found")
        fields(params, {"session_id", "process_id"}, optional[method])
        pid = identifier(params["process_id"], "process_id")
        entry = self._entries.get((sid, pid))
        if entry is None:
            raise error("not_found", "Process not found")
        if method == "process.release":
            await self._release(entry)
            return {"released": True}
        if entry.info["state"] == "released":
            raise error("gone", "Process output has been released")
        if method == "process.wait":
            return await self._wait(entry, params)
        if method == "process.kill":
            wait_ms = integer(params.get("wait_ms", 5000), "wait_ms", maximum=30_000)
            self._request_kill(entry)
            if wait_ms:
                try:
                    async with asyncio.timeout(wait_ms / 1000):
                        await entry.done.wait()
                except TimeoutError:
                    pass
            return dict(entry.info)
        if entry.info["mode"] != "pty" or entry.done.is_set() or entry.master < 0:
            raise error("conflict", "Process has no running PTY")
        if method == "process.resize":
            rows = integer(params.get("rows"), "rows", 1, 1000)
            cols = integer(params.get("cols"), "cols", 1, 1000)
            fcntl.ioctl(entry.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            return dict(entry.info)
        data = decode_chunk(params.get("data_base64"))
        accepted = 0
        async with entry.write_lock:
            try:
                async with asyncio.timeout(5):
                    while accepted < len(data):
                        try:
                            accepted += os.write(entry.master, data[accepted:])
                        except BlockingIOError:
                            await self._ready(entry.master, write=True)
            except (OSError, TimeoutError) as exc:
                raise error("io_error", "PTY input failed", accepted_bytes=accepted) from exc
        return {"accepted_bytes": accepted}

    async def _start(self, params: JsonObject) -> JsonObject:
        fields(
            params,
            {"session_id", "process_id", "mode", "argv"},
            {"cwd", "env", "rows", "cols", "wait_ms"},
        )
        sid = session_identifier(params["session_id"])
        pid = identifier(params["process_id"], "process_id")
        mode = params["mode"]
        if not isinstance(mode, str) or mode not in {"stdio", "pty"}:
            raise invalid("mode must be stdio or pty")
        argv_value = params["argv"]
        if not isinstance(argv_value, list) or not argv_value or len(argv_value) > 4096:
            raise invalid("argv must be a nonempty array")
        if any(not isinstance(arg, str) or "\0" in arg for arg in argv_value):
            raise invalid("argv must contain strings without NUL")
        argv = cast(list[str], argv_value)
        string(argv[0], "argv[0]")
        env = safe_env(params.get("env", {}))
        wait_ms = integer(params.get("wait_ms", 1000), "wait_ms", maximum=30_000)
        rows = integer(params.get("rows", 24), "rows", 1, 1000)
        cols = integer(params.get("cols", 80), "cols", 1, 1000)
        if mode == "stdio" and ("rows" in params or "cols" in params):
            raise invalid("stdio does not use window dimensions")
        default_cwd = await self.store.session_cwd(sid, require_token=True)
        cwd = Path(string(params["cwd"], "cwd")) if "cwd" in params else default_cwd
        if not cwd.is_absolute():
            cwd = default_cwd / cwd
        signature = fingerprint(
            {
                "mode": mode,
                "argv": argv_value,
                "cwd": str(cwd),
                "env": cast(JsonValue, env),
                "rows": rows,
                "cols": cols,
            }
        )
        async with self._begin_lock:
            if self._closing:
                raise error("offline", "Process manager is closing")
            await self.store.session_cwd(sid, require_token=True)
            entry = self._entries.get((sid, pid))
            if entry is not None:
                if entry.signature != signature:
                    raise error("conflict", "Process ID has different startup parameters")
                if entry.info["state"] == "released":
                    raise error("gone", "Process has been released")
            else:
                if self.active_count >= 16:
                    raise error("resource_limit", "At most 16 processes may run")
                info: JsonObject = {
                    "session_id": sid,
                    "process_id": pid,
                    "mode": mode,
                    "cwd": str(cwd),
                    "state": "starting",
                    "exit_code": None,
                    "output_complete": False,
                    "error": None,
                }
                entry = _Process(signature, info, default_cwd.parent / "processes" / pid)
                entry.sizes = {"pty": 0} if mode == "pty" else {"stdout": 0, "stderr": 0}
                child_env = {
                    **self.child_env,
                    **env,
                    "KAPY_MACHINE_ID": self.store.machine_id,
                    "KAPY_SESSION_ID": sid,
                    "KAPY_SESSION_TOKEN": self.store.session_token(sid),
                    "KAPY_DAEMON_SOCKET": str(self.store.paths.socket_path),
                }
                await self._save(entry)
                self._entries[sid, pid] = entry
                # Spawn belongs to the domain operation, never a connection handler.
                await self._spawn(entry, argv, cwd, child_env, rows, cols)
        return await self._wait(entry, {"wait_ms": wait_ms})

    async def _spawn(
        self, entry: _Process, argv: list[str], cwd: Path, env: dict[str, str], rows: int, cols: int
    ) -> None:
        slave = -1
        try:
            if entry.info["mode"] == "stdio":

                def open_spools() -> None:
                    secure_directory(entry.root)
                    for name in ("stdout", "stderr"):
                        entry.fds[name] = os.open(
                            entry.root / name,
                            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC,
                            0o600,
                        )

                await self.store.io.run(open_spools)
                entry.child = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    start_new_session=True,
                    limit=CHUNK_SIZE,
                )
            else:
                entry.master, slave = os.openpty()
                os.set_blocking(entry.master, False)
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                entry.child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(Path(__file__).with_name("_pty_exec.py")),
                    *argv,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=cwd,
                    env=env,
                    start_new_session=True,
                )
            entry.meta["pid"] = entry.child.pid
            entry.meta["identity"] = await self.store.io.run(_identity, entry.child.pid)
            entry.meta["boot_id"] = await self.store.io.run(_boot_id)
            entry.info["state"] = "running"
            await self._save(entry)
            entry.task = asyncio.create_task(self._collect(entry), name="process-collect")
        except Exception:
            try:
                if entry.child is not None:
                    _kill_group(entry.child.pid)
                    transport = getattr(entry.child, "_transport", None)
                    if transport is not None:
                        transport.close()
                    await entry.child.wait()
            finally:
                entry.info.update(
                    state="failed", error={"kind": "io_error", "message": "Process startup failed"}
                )
                try:
                    self._close_fds(entry)
                    await self._save(entry)
                finally:
                    # A broken database must not strand an entry without a
                    # collector: shutdown/release still need a terminal event.
                    entry.done.set()
                    entry.changed.set()
        finally:
            if slave >= 0:
                os.close(slave)

    @staticmethod
    async def _ready(fd: int, *, write: bool = False) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def ready() -> None:
            if not future.done():
                future.set_result(None)

        if write:
            loop.add_writer(fd, ready)
        else:
            loop.add_reader(fd, ready)
        try:
            await future
        finally:
            if write:
                loop.remove_writer(fd)
            else:
                loop.remove_reader(fd)

    async def _drain(self, entry: _Process, name: str) -> None:
        assert entry.child is not None
        stream = getattr(entry.child, name) if name != "pty" else None
        while True:
            if name == "pty":
                try:
                    data = os.read(entry.master, CHUNK_SIZE)
                except BlockingIOError:
                    await self._ready(entry.master)
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        return
                    raise
            else:
                assert stream is not None
                data = await stream.read(CHUNK_SIZE)
            if not data:
                return
            if name == "pty":
                entry.tail = (entry.tail + data)[-8192:]
            else:

                def append(data: bytes = data) -> None:
                    view = memoryview(data)
                    offset = 0
                    while offset < len(view):
                        offset += os.write(entry.fds[name], view[offset:])

                await self.store.io.run(append)
            entry.sizes[name] += len(data)
            entry.last_output = time.monotonic()
            entry.changed.set()
            if name == "pty":
                await self._save(entry)

    async def _collect(self, entry: _Process) -> None:
        assert entry.child is not None
        drains = [asyncio.create_task(self._drain(entry, name)) for name in entry.sizes]
        try:
            # No normal-run timeout: same-group descendants may keep producing
            # after the leader exits. Full output takes precedence over early exit.
            await asyncio.gather(*drains)
            entry.info["exit_code"] = await entry.child.wait()
            while _group_alive(entry.child.pid):  # noqa: ASYNC110 - kernel has no group-exit event
                await asyncio.sleep(0.05)
            # Persist output before the durable terminal record claims it is
            # complete. Failed fsync follows the explicit incomplete path.
            for fd in entry.fds.values():
                await self.store.io.run(os.fsync, fd)
        except Exception, asyncio.CancelledError:
            entry.complete = False
            for task in drains:
                task.cancel()
            await asyncio.gather(*drains, return_exceptions=True)
            _kill_group(entry.child.pid)
            # Close asyncio pipes so an escaped descriptor cannot stall wait().
            transport = getattr(entry.child, "_transport", None)
            if transport is not None:
                transport.close()
            await entry.child.wait()
            entry.info["exit_code"] = entry.child.returncode
            entry.info["error"] = {"kind": "io_error", "message": "Output collection interrupted"}
        finally:
            entry.info["state"] = (
                "killed"
                if entry.info["state"] == "killing"
                else "exited"
                if entry.complete
                else "failed"
            )
            entry.info["output_complete"] = entry.complete
            self._close_fds(entry)
            try:
                await self._save(entry)
            finally:
                entry.done.set()
                entry.changed.set()

    @staticmethod
    def _close_fds(entry: _Process) -> None:
        for fd in entry.fds.values():
            os.close(fd)
        entry.fds.clear()
        if entry.master >= 0:
            os.close(entry.master)
            entry.master = -1

    def _request_kill(self, entry: _Process) -> None:
        if entry.done.is_set() or entry.cleanup is not None:
            return
        entry.info["state"] = "killing"
        entry.cleanup = asyncio.create_task(self._kill(entry), name="process-kill")

    async def _kill(self, entry: _Process) -> None:
        if entry.child is not None:
            _kill_group(entry.child.pid)
        try:
            async with asyncio.timeout(2):
                await entry.done.wait()
        except TimeoutError:
            if entry.task is not None:
                entry.task.cancel()
                await asyncio.gather(entry.task, return_exceptions=True)

    async def _wait(self, entry: _Process, params: JsonObject) -> JsonObject:
        wait_ms = integer(params.get("wait_ms", 1000), "wait_ms", maximum=30_000)
        max_bytes = integer(params.get("max_bytes", CHUNK_SIZE), "max_bytes", 1, CHUNK_SIZE)
        cursor = params.get("cursor", {name: 0 for name in entry.sizes})
        if not isinstance(cursor, dict) or cursor.keys() != entry.sizes.keys():
            raise invalid("cursor must match process mode")
        positions = {
            name: integer(cursor[name], name, maximum=size) for name, size in entry.sizes.items()
        }
        deadline = time.monotonic() + wait_ms / 1000
        reason = "snapshot" if not wait_ms else "timeout"
        while wait_ms and not entry.done.is_set():
            now = time.monotonic()
            if now >= deadline:
                break
            quiet_at = entry.last_output + 0.2
            has_output = any(entry.sizes[name] > position for name, position in positions.items())
            if entry.info["mode"] == "pty" and has_output and now >= quiet_at:
                reason = "quiet"
                break
            entry.changed.clear()
            delay = (
                min(deadline - now, max(0.001, quiet_at - now))
                if entry.info["mode"] == "pty" and has_output
                else deadline - now
            )
            try:
                async with asyncio.timeout(delay):
                    await entry.changed.wait()
            except TimeoutError:
                pass
        if wait_ms and entry.done.is_set():
            reason = "exited"
        if entry.child is not None and entry.child.returncode is not None:
            entry.info["exit_code"] = entry.child.returncode
        output: JsonObject = {"kind": entry.info["mode"]}
        async with entry.read_lock:
            if entry.info["state"] == "released":
                raise error("gone", "Process output has been released")
            for name, position in positions.items():
                available = entry.sizes[name]
                start = max(position, available - len(entry.tail)) if name == "pty" else position
                if name == "pty":
                    data = entry.tail[start - (available - len(entry.tail)) :][:max_bytes]
                else:

                    def read(
                        name: str = name, start: int = start, available: int = available
                    ) -> bytes:
                        try:
                            with (entry.root / name).open("rb") as stream:
                                stream.seek(start)
                                return stream.read(min(max_bytes, available - start))
                        except FileNotFoundError:
                            if available == 0:
                                return b""
                            raise

                    data = await self.store.io.run(read)
                output[name] = byte_chunk(
                    data,
                    start,
                    available,
                    eof=entry.done.is_set() and start + len(data) >= available,
                    truncated=start > position,
                )
        return {"process": dict(entry.info), "reason": reason, "output": output}

    async def _release(self, entry: _Process) -> None:
        if not entry.done.is_set():
            raise error("conflict", "Running process cannot be released")
        async with entry.read_lock:
            # Persist the tombstone first; interruption leaves only harmless files.
            entry.info["state"] = "released"
            entry.tail = b""
            await self._save(entry)

            await self.store.io.run(_remove_root, entry.root)

    async def release_session(self, session_id: str) -> None:
        async with self._begin_lock:
            entries = [entry for (sid, _), entry in self._entries.items() if sid == session_id]
            for entry in entries:
                self._request_kill(entry)
            await asyncio.gather(*(entry.done.wait() for entry in entries))
            for entry in entries:
                await self._release(entry)

    async def aclose(self) -> None:
        self._closing = True
        async with self._begin_lock:
            for entry in self._entries.values():
                self._request_kill(entry)
        await asyncio.gather(*self._operations, return_exceptions=True)
        await asyncio.gather(*(entry.done.wait() for entry in self._entries.values()))
        await asyncio.gather(
            *(entry.cleanup for entry in self._entries.values() if entry.cleanup is not None),
            return_exceptions=True,
        )
