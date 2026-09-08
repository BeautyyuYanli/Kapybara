# Agent and Skills modules

`kapy.agent.Runner` is an async State `SessionRunner`. Construct it with explicit
`RunnerConfig`, Gateway's `MachineCaller`, borrowed `ModelBackend`, initialized
`AgentPayloadStore`, and Gateway's `AuthorizeWait` callback. Construction starts no
I/O. The agent does not load environment files or own these resources.

At session creation, call `Runner.initial_state(instructions=..., skills=await
skills.catalog())`. Store that snapshot in State's `SessionSpec.initial_state`.
User instructions and the catalog remain fixed for the session. Current behavior instructions,
including the history view schema, session ID and machine selection, are supplied each run.
A recognized old base prompt is replaced without rewriting the stored user/skill snapshot.

`RunnerConfig` contains the resolved model name/window/output and compression/media settings,
not credentials. Gateway derives it from the session model ID and current provider catalog.
`ModelConnection(type, base_url, api_key)` is an immutable borrowed connection;
`create_model_backend(connection, http_client)` selects native Pydantic OpenAI Responses,
Chat, or Google adapters. `ModelBackend.create_model(name)` and `classify_error(error)`
remain replaceable interfaces. Runner never closes the shared HTTP client. Provider failures
are sanitized by Gateway before State persists them. Detailed resource and budget precedence
is in [models](models.md).

## Persistence and recovery

The runner uses the actual `kapy.state` objects directly. Complete responses commit
before tools execute, tool intents commit before RPC dispatch, and complete returns
commit before the next model request. Raw history messages commit before projection
compression. The final checkpoint is the next number and is returned only in
`RunResult`; State commits it with waiting, output, subscriptions, and completion.
Reserved inputs are individually included and consumed in checkpoints. Polling and
Pydantic's enqueue support insert steer at model/tool boundaries and continue a run
that would otherwise finish with newly reserved input.

Pydantic AI 2.40 uses capabilities and the async-context-manager form of
`run_stream_events`. Runtime message history is a projection; `new_messages()` is
not used as the raw archive. Complete `ModelResponse.usage.input_tokens +
output_tokens` observations drive compression. Cached input is already included;
it is neither subtracted nor added again. Cumulative `result.usage` is a report,
never a context-size measurement. Missing/all-zero usage is unknown.

Each fresh high-usage response permits one persisted sweep. Old closed cycles move
0 → omitted tool results → inputs/final output → removed. The newest complete
interaction blocks, approximately 10% by block count, remain protected. A current
cycle can only omit older completed tool returns. Tool batches stay paired.
Explicit provider context-length rejection allows at most two extra compression
retries; checkpoints retain that retry count across recovery.

`AgentPayloadStore` borrows a PostgreSQL pool. Its table has no State foreign key;
immutable bytes are addressed by `(session_id, sha256)`. Media bytes are saved
before references commit. Message encoding retains media positions and types in
`ToolReturnPart.metadata.kapy_media_refs`; hydration restores `BinaryContent` from
PostgreSQL. Large context projections and pending transfer chunks also use payload
references. Media rejection changes only the model projection to an explanatory
text result with the same call ID; it never rereads the machine file. HTTP media
errors are classified separately from authentication, network, and context errors.

Gateway must durably schedule `delete_session(session_id)` after State has stopped
the runner and deleted the session, and retry interrupted cleanup. Normal shutdown
preserves payloads. Payload writes finish before runner cancellation returns.

| Boundary | Limit |
| --- | --- |
| Complete message, including State envelope | 256 KiB JSON |
| Delta | 16 KiB JSON |
| Checkpoint | 4 MiB JSON |
| Inline context projection | 2 MiB JSON |
| Durable payload | 64 MiB |
| Media file | Configurable, at most 20 MiB |
| File/terminal chunk | 65,536 decoded bytes |

These are storage/transport budgets, never token estimates. Oversized complete
model messages fail explicitly; tool argument JSON is never cut and then executed.
Process output has per-stream byte cursors and incremental UTF-8 decoder state;
bounded displays include references to the machine's releasable output spool.

## Tools and apply_patch

Machine tools call only the approved `process.*` and `file.*` RPCs, always passing
the State session ID and `timeout=60.0`. Model parameters cannot supply credentials,
session IDs, process-start IDs, or transfer IDs. Stable process/transfer UUIDs derive
from session, State run, tool-call ID, and substep. Recovery observes known handles;
an unconfirmable outcome remains `outcome_unknown` at the original call ID.
Interrupted terminal writes are never automatically replayed. Built-in model tools are
process operations, read_media and wait. Ordinary text files use shell commands rather
than extra file_read/file_write tools. An unfinished call to a removed tool gets an
`outcome_unknown` return without replay; completed historical tool results remain intact.
PTY/stdout/stderr cursor schemas specify byte positions. A display-truncated stdio chunk
can be reread from its start with a smaller `max_bytes`; the full spool remains until release.

`ScriptTool` takes a Pydantic parameter model and a renderer returning
`ProcessCommand(argv, stdin, cwd)`, plus an optional async `prepare(host, command)`
returning a prepared command. `ScriptHost` exposes only `workspace()`, `run(argv)` and
`push(path, bytes)`. Preparation run must reach a confirmed successful exit; otherwise
the known process is retained and no script is launched. The host owns stable IDs,
checkpointed transfer/process steps and unknown-outcome recovery. The wrapper validates parameters itself because
`Tool.from_schema` publishes schema without validating it. `machine_id` is reserved.
Stdin is uploaded as bytes and redirected with the fixed argv:

```python
["/bin/sh", "-c", 'exec "$@" < "$0"', absolute_stdin_path, *command.argv]
```

Input files are removed only after process termination is confirmed. Running or
unknown outcomes retain them until later cleanup or machine-session deletion.

Runner registers only the supplied plugins; an empty sequence adds none. The Gateway
uses `apply_patch_plugin()` by default, configured through `KAPY_TOOL_PLUGINS`; explicit
`create_app(..., plugins=())` disables it and supplied definitions replace the default list.
The apply_patch plugin uses the verified upstream `rust-v0.153.4` release
from commit `8639ac2d93442bcec5631b693b4ed7c0144422b7`. Regenerate resources with:

```sh
uv run --locked python -m kapy.agent.generate_apply_patch
```

The generator verifies both Linux bundle SHA-256 values and preserves upstream
binary/license/source bytes. Its generated `.gitattributes` disables newline
conversion. The model description changes only the FREEFORM transport wording to
a JSON `patch` field and retains the complete grammar/examples. Installation uses
managed `pwd`, `uname -m`, executable SHA-256 verification, `mkdir`, file transfers,
and `chmod` commands; no binary
is run on the control host. Skill path instructions are not an OS sandbox.

## Skills service

`SkillService(pool, schema=...)` borrows Gateway's metadata pool and exposes
`initialize`, `create`, `update`, `delete`, `get`, `catalog`, and `download`.
Metadata and exact ZIP bytes share a PostgreSQL row. Mutations and replay receipts
commit in one transaction. Gateway scopes the internal `request_key` by identity;
update/delete require `expected_revision`. Identical retries return the first
result even after later changes or deletion. Catalog uses literal case-insensitive
substring matching across ID/name/description, ordered by ID.

ZIP limits are 16 MiB compressed, 128 MiB expanded, 4096 entries, 32 MiB per file,
and 64 KiB UTF-8 SKILL.md. Validation rejects path traversal, duplicate/conflicting
paths, links, special files/permissions, YAML aliases, and invalid frontmatter.
`pack_skill` and `extract_skill` share validation and preserve regular executable
bits. Extraction publishes a prepared tree into a new destination. Async callers
should invoke these synchronous helpers in a bounded thread; the service limits
concurrent archive validation to two tasks. Gateway owns transport chunking and
authorization; no second transfer API is introduced.

## Verification

Module tests use real PostgreSQL with independent random schemas for archive CRUD,
revision races, replay conflicts, transaction rollback, and payload isolation.
Recovery tests serialize media and external context references, close and rebuild
the pool, payload store, and Runner, then verify original media hydration and
explicit missing/corrupt reference failures without rereading the machine.
Agent tests use model and State test doubles, including dummy-key
`httpx2.MockTransport` with the actual OpenAI Chat streaming adapter. They cover
media refusal, steer, waiting authorization, cancellation recovery, concurrent
session isolation, and usage-driven compression across restart.

Run the real Execution manager acceptance tests with:

```sh
uv run --locked python tests/agent/run_docker_acceptance.py
```

The local Git repository must contain commit `27a34dd`, and the local Docker image
`kapy-v2-machine:dev` must be available. The launcher exports that committed
Execution snapshot and mounts it read-only alongside this worktree's Agent and
Skills source in a disposable container with bounded memory and process counts.
It uses the actual `ExecutionStore` and `MachineService` to verify plugin
installation, upload/download transfers, fixed stdin redirection, add/update
behavior, process exits, and input-file cleanup. Assertions at RPC dispatch verify
that complete tool calls and stable operation parameters are checkpointed;
assertions at the next model request verify that tool results are checkpointed.
The same Docker suite checks that interrupted extraction publishes no partial
destination, removes its staging tree, and preserves existing content.

The Docker suite still uses model and State test doubles. It makes no real model
requests or Telegram sends, mounts no environment files or Docker socket, and
does not test the daemon's network transport. The separate
`tests/agent/docker_verify_patch.py` helper checks the pinned binary directly;
the manager acceptance suite supplies the Agent-to-manager integration evidence.
