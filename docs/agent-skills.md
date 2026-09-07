# Agent and Skills modules

`kapy.agent.Runner` is an async State `SessionRunner`. Construct it with explicit
`RunnerConfig`, Gateway's `MachineCaller`, borrowed `httpx2.AsyncClient`, initialized
`AgentPayloadStore`, and Gateway's `AuthorizeWait` callback. Construction starts no
I/O. The agent does not load environment files or own these resources.

At session creation, call `runner.initial_state(instructions=..., skills=await
skills.catalog())`. Store that snapshot in State's `SessionSpec.initial_state`.
Instructions and the catalog remain fixed for the session. Each run selects the
optional `session.config.model`, falling back to `RunnerConfig.model`.

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
Interrupted terminal writes are never automatically replayed.

`ScriptTool` takes a Pydantic parameter model and a renderer returning
`ProcessCommand(argv, stdin, cwd)`. The wrapper validates parameters itself because
`Tool.from_schema` publishes schema without validating it. `machine_id` is reserved.
Stdin is uploaded as bytes and redirected with the fixed argv:

```python
["/bin/sh", "-c", 'exec "$@" < "$0"', absolute_stdin_path, *command.argv]
```

Input files are removed only after process termination is confirmed. Running or
unknown outcomes retain them until later cleanup or machine-session deletion.

The built-in apply_patch plugin uses the verified upstream `rust-v0.153.4` release
from commit `8639ac2d93442bcec5631b693b4ed7c0144422b7`. Regenerate resources with:

```sh
uv run --locked python -m kapy.agent.generate_apply_patch
```

The generator verifies both Linux bundle SHA-256 values and preserves upstream
binary/license/source bytes. Its generated `.gitattributes` disables newline
conversion. The model description changes only the FREEFORM transport wording to
a JSON `patch` field and retains the complete grammar/examples. Installation uses
managed `pwd`, `uname -m`, `mkdir`, file transfers, and `chmod` commands; no binary
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

## Verification and remaining integration boundary

Module tests use real PostgreSQL with independent random schemas for archive CRUD,
replay/revision checks, and payload isolation. Agent tests use dummy-key
`httpx2.MockTransport` with the actual OpenAI Chat streaming adapter and fake
machine callers. They cover media refusal, steer, waiting authorization, cancellation
recovery, compression, durable codec, large transfer references, and script argv.

`tests/agent/docker_verify_patch.py` must run only inside a disposable Docker
machine. It verifies the pinned binary, stdin redirection, add/update behavior, and
literal shell-looking input. This is separate from the pending end-to-end test
through Execution's actual process manager. That integration requires its owner to
publish the concrete handler; the module does not supply a substitute manager.
No real model requests or Telegram messages are part of these tests.
