# Independent process service

`open_process_manager` owns one Linux manager, its SQLite engine, state-directory
lock and background GC. It has no dependency on machine, scope, session or RPC
models. It is not wired into the existing application.

```python
from pathlib import Path
from uuid import uuid4

from kapy.tmpv2.processes import ProcessSpec, open_process_manager

async with open_process_manager(child_env={"PATH": "/usr/bin:/bin"}) as manager:
    process_id = uuid4()
    await manager.start(process_id, ProcessSpec(argv=("pwd",), cwd=str(Path.cwd())))
    result = await manager.wait(process_id)
    output = await manager.read_output(process_id, stream="stdout")
    await manager.release(process_id)
```

`start` accepts a new process ID and returns its starting snapshot. Any existing
ID conflicts, even with identical parameters. A canceled observer cannot cancel
an accepted start; use `get` to inspect its ID. The environment is the supplied
base plus the command's `env`, never the service's inherited environment.
`cwd` must be an existing absolute directory; the service never owns or deletes it.
`created_at` records when the startup request was accepted.

`state` describes the direct child. Normal exit does not kill descendants that
still hold output pipes. `wait` waits for both the child's terminal state and the
end of output collection; its timeout returns the current snapshot. A nonzero
exit code is still `exited`. `terminate` returns after accepting the stop intent;
it remains available after child exit while output is still collecting. It signals
the ordinary process group with SIGTERM, escalating after two seconds.
A further two-second drain deadline bounds
output retained by escaped descendants, recording `incomplete` if necessary.
There is no process-group liveness polling or containment of escaped processes.
Management failure while the child is running records `failed`, even if cleanup
then kills it. A failure after the child has exited preserves its exit result.
Finalization submits the terminal fields again so a failed earlier exit-state
write cannot leave a completed execution recorded as running.

Stdio closes stdin and saves separate full `stdout` and `stderr` files. PTY mode
establishes a controlling terminal and saves the full merged `pty` output in a
file through the same collector. `write` makes one nonblocking write and returns
the actual accepted byte count (possibly zero); callers own any unsent remainder.
It never retries after a partial write or cancellation. Both modes expose byte
cursors with pages up to 65536 bytes. Output is fsynced before recording `complete`.

Storage defaults to `$XDG_STATE_HOME/kapy` (or `~/.local/state/kapy`). Override it
with an absolute `state_dir`. Newly created directories and files are private;
an existing state directory must already be private and owned by the current UID.
The service uses `processes.sqlite3`, `processes.lock`, and
`processes/<process_id>/<stream>` under that directory. SQLModel defines one table
with its own metadata and the default schema. SQLite uses WAL and synchronous
FULL; ORM-loaded UTC times are made timezone-aware at the DTO boundary.

`release` requires terminal child state and finished output collection, otherwise
it raises `conflict`. It only marks the process `deleting`; repeats return that
snapshot, and an absent row returns `None`. GC deletes its output directory
and then commits deletion of the row. A missing directory is success; failed
physical or database deletion leaves the row for retry. The context performs a
startup sweep, then sweeps every five seconds. Once GC removes the row, its ID can
be reused. Closing the context stops execution and preserves un-released results.
Startup marks interrupted execution `lost` and interrupted output `incomplete`.
It does not adopt, replay, identify or kill processes from a previous runtime;
`lost` does not prove that an old OS process has exited. Execution failure logs
include the process ID, stage, exception class and available errno, without raw
exception text. Command parameters, environment, error text and OS identities are
not stored in process rows or returned in status DTOs.

`get` and `list` include deleting rows. `read_output` raises `gone` while deletion
is pending; after GC removes the row, reads raise `not_found`. `list` orders IDs
by ascending UUID, excludes the supplied `after` ID, and returns `next_after` only
when another page exists.

The repository owns short database transactions; the manager owns OS handles and
active tasks; `gc_processes` only borrows a repository and output directory.
Blocking filesystem operations finish before their dependent descriptors close.
The context joins GC, accepted operations and execution before disposing the
engine and releasing the lock, including initialization and cancellation failures.

Real process tests run in the disposable Docker machine, with source and tests
mounted read-only and writable fixtures in its temporary directory:

```sh
docker build -f Dockerfile.machine -t kapy-v2-machine:process-manager .
docker run --rm --init --network none --memory 1g --pids-limit 128 \
  -e PYTHONPATH=/workspace/src -w /workspace \
  -v "$PWD/src:/workspace/src:ro" -v "$PWD/tests:/workspace/tests:ro" \
  -v "$PWD/pyproject.toml:/workspace/pyproject.toml:ro" \
  kapy-v2-machine:process-manager /app/.venv/bin/python -m pytest \
  -q -p no:cacheprovider tests/tmpv2/processes
```
