# Kapy v2 delivery ledger

## Current scope and user decisions

Implement the entire kapy_v2.md system. The lead owns scaffolding, architecture,
integration, acceptance and branch merges. Domain implementations belong to four
Lody worktree seniors. Each completed cmd-proposal with exactly one full-context
Mei simplification review; each is approved for cmd-impl and owns its persistent
Elysia plus all three Eden review groups/five stages. Do not manage grandchildren
or personally fix domain code. Never manually edit generated files; use uv for lock.

Latest real-user corrections were discovered in current Lody history at 12:30 UTC:
- Machine research/process/file tests must run in Docker, not the host.
- Perfect process-tree cleanup is unnecessary: process groups/best effort suffice.
- Kapy v2 is the product; Lody is only the coordination tool. Do not study its runtime.
- Token counts must use provider API usage, never tiktoken or local token estimates.

No real Telegram sends were authorized. Use fake Bot API for outbound tests and
polling/setup; do not start the real Telegram plugin during unattended acceptance.
Live harmless model inference is authorized. .env contains real supplied provider/TG
values plus generated local control/signing/docker-machine credentials; mode 600,
ignored. Never print or commit secrets. Separate new-task-generated credentials are
safe blank examples in .env.example.

## Seniors and current operations

Worktree root: /home/beautyyu/.lody/repos/local---d9a2a3ade7e6/worktrees/<session-id>
Lead: /home/beautyyu/Development/kapy_v2, main.
Root Lody session: 05144158-4e23-407a-ba4f-563faa33aafa.

| Owner | Session ID | Branch | Current scope |
| --- | --- | --- | --- |
| Execution | 515746bc-f9e2-4d09-a894-65067a65f369 | feat/kapyrpc | Complete execution/rpc cmd-impl |
| State | 37703573-1ccd-4f8b-a137-ba52ca9e8e60 | feat/kapy | Complete state cmd-impl including approved additions |
| Intelligence | a88a199c-41c1-49d6-ad09-2d5b87f80b12 | feat/kapy-agentskills | Complete agent/skills cmd-impl |
| Gateway | 0f9cff83-dff7-4881-b5a1-a6b1e761dd27 | feat/kapycli | Complete gateway/cli/settings/TG cmd-impl |

Current full consolidated implementation operations (all approvals included):
- kapy-v2-consolidated-implementation-20260907T1300: Execution, Intelligence, Gateway;
  actually created 13:01 UTC, deadline 17:01 UTC.
- kapy-state-consolidated-implementation-20260907T1304: State; created 13:03 UTC,
  deadline 17:03 UTC. Includes wait_submission/export_history/update-delete UUID
  requests and environment-overridable test database URLs.

Important Lody scheduling behavior: session_chat appends asynchronous queued turns;
messages are not reliable live steering. Seniors have processed old proposal-only
instructions even after newer implementation approvals. At ~12:59 the lead cancelled
known old active operations and current turns, then sent the consolidated tasks above.
Cancellation is best effort and did not remove all previously queued prompts: late
old proposal-only replies still arrived through 13:07. Do not repeatedly send more
approvals or reopen design review in response. Preserve the consolidated decisions.
Do not poll operation_get in a loop. Current-session lody_session_history occasionally
reveals real-user steering and senior milestones that native turn delivery hides.
Read and filter recent user entries; distinguish actual user steering from senior
reports. No newer actual user correction was seen through the last read at ~13:08.

## Authoritative contracts

Read docs/contracts.md. It overrides older proposal drafts and cgroup assumptions.

- State UUID DTOs and one SessionRunner(RunContext)->RunResult; no ACL/parent/scope
  inside State. Gateway owns identity, creator/direct-child/channel grants, request
  ownership and durable cleanup. State owns its own pool/Valkey/runner lifecycle.
- State wait_submission, snapshot export_history, update/delete request_id are fully
  approved from proposal 3a3a10e. Gateway must call wait_submission directly instead
  of scanning output to reconstruct completion. Pages 512 KiB actual JSON, records
  256 KiB, deltas 16 KiB, checkpoints 4 MiB.
- RpcPeer async context callbacks, dispatch_json(str,handler)->str|None, MachineCaller
  call(...,*,timeout=60), ProxyAuth/call_local_proxy/resolve_paths. process.start(mode),
  process.wait(wait_ms=0 for immediate read), write/resize/kill/list/release; file
  push/pull/chunk/finish/abort; session.ensure/release. No process.run/read or file
  stat/read/write compatibility RPCs. No DaemonConfig cgroup_root/delegation.
- Runner(config,machine_caller,*,http_client,payload_store,authorize_wait,plugins=()).
  The only initialization entry is runner.initial_state(*,instructions,skills).
  Do not adopt the old queued initialize_session proposal. __call__ uses State types.
  session.config.model may override default model per run. Instructions snapshot at
  creation/new; no hot update. authorize_wait(UUID,tuple[UUID,...]) async, rejects
  via PermissionError. Gateway supplies it; State subscribes and emits completion.
- AgentPayloadStore borrows Gateway metadata pool, initialize/put/get/delete_session;
  immutable session+SHA256 bytes, no State FK or state_schema parameter. Store before
  checkpoint; Gateway durable cleanup after State has stopped/deleted session.
  Media 20 MiB, payload 64 MiB, contexts above 2 MiB may use a stored reference.
- Compression uses latest response input_tokens+output_tokens, not aggregate usage;
  one sweep per fresh usage, no stale repeated degradation. Latest ~10% by complete
  interaction blocks, not claimed token estimate. Initially unknown usage; explicit
  context-too-long permits at most two checkpointed compression retries. No tiktoken
  or byte/media-based token estimate/reserve. A queued older input-only proposal
  cf2962f is superseded by the consolidated implementation instruction.
- SkillService borrows Gateway metadata pool, initialize, no lifecycle factory. CRUD
  internal scoped request_key plus expected_revision, external UUID request_id.
  SkillDescription(id,description), pack_skill/extract_skill shared with CLI. 16 MiB
  ZIP, existing Execution 64 KiB file transfers, no second transfer subsystem.
- UnsafeQuery/QueryLimitExceeded may use -32040/-32041; Execution -32020/-32021 remain
  resource_limit/io_error. Other conflict/not-found/invalid mappings unchanged.

## Main and verified milestones

Main contains scaffolding, State public types, approved proposals, acceptance scripts,
and actual RPC/XDG modules. There is still no complete runnable application.
- State contracts original 4b42349 merged as 090c122; final proposal 3a3a10e merged.
- Intelligence bf1f55f proposal merged as 5d052d9. Later old queued doc churn is not
  a new implementation blocker; consolidated instructions freeze final differences.
- Execution public modules 1091292 independently read and container-tested (46 pass).
  Cherry-picked as 8e83fe4; one proposal-only conflict kept main's version. Full
  daemon/process/file implementation is not merged. Ruff/pyrefly on main pass.
- State implementation e297398 remains unmerged and under senior cmd-impl. Lead
  created read-only snapshot .local/acceptance/state-e297398 via git archive.
  It lacks approved receipt/export/update-delete additions; State consolidated task
  explicitly requires them before final delivery. Existing test fixtures hardcode
  host service URLs; senior must allow KAPY_DATABASE_URL/KAPY_VALKEY_URL overrides.

## Environment and acceptance

Python 3.14.4 host / 3.14.3 container, uv 0.11.13. Main pyproject/lock managed by lead.
Pydantic AI 2.40.0, httpx2 2.12.0, FastAPI 0.141.1; Ruff/pyrefly installed.
Compose kapy-v2: PostgreSQL17 localhost55432 and Valkey8 localhost56379, healthy.
Do not restart/flush shared services during senior tests; use unique schema/namespace.

Docker images kapy-v2:dev scaffold and kapy-v2-machine:dev (dev tools) built. Compose
--profile dev machine is running with read-only src/tests/scripts/pyproject mounts,
2 GiB and 256 PID limits, own init/PID namespace, no .env or Docker socket mount.
Containers on kapy-v2_default use postgres:5432 and valkey:6379. Independent senior
containers mount their source/tests and run /app/.venv/bin/pytest with
PYTHONPATH=/workspace/src. Rebuild images with uv when dependencies change.

- uv sync --locked, scaffold uv build/Docker build passed; not full app evidence.
- scripts/check_provider.py passed live model: 1 harmless tool, 2 requests, exact marker.
- scripts/check_system.py: live HTTP control-to-Docker-machine acceptance client,
  stable create retry + unpredictable random tool marker compared with final reply
  and durable history, then session deletion. Ruff/pyrefly pass; NOT run yet because
  complete app not landed. Verify actual Completion/history shapes when integrated.
- scripts/bench_state.py: explicit migrate then unique schema, fake runner 2ms sleep.
  e297398 snapshot in Docker: 100 sessions / 2000 inputs accepted, completed and
  replayed after reopening; exact ordering/no duplicates/no per-session overlap.
  15.561s, 128.52inputs/s, submit p50 623.59ms, p95 1074.39ms, peak 2 fake runners.
  docs/acceptance-results.md records scoped baseline, not production guarantee.
- scripts/bench_events.py added: early backlog reaches first later listener, 100
  subscribers receive two broadcasts, event_id/sequence checks. Uses State event
  envelope (not raw string payload) and trusted producer_session_id=None. Passed:
  each broadcast delivered100; publish33.63/34.79ms, all completed1016.36/1077.33ms.
  The run completed and its schema was cleaned; no running exec session remains.
- Execution 1091292 snapshot in .local/acceptance/execution-1091292: 46 RPC/XDG tests
  pass in Docker, 0.15s. Main Ruff and pyrefly pass after integration.

## Remaining lead work

Wait for actual domain deliveries and senior five-stage reports. Keep current
consolidated scope while queued old messages drain. Integrate reviewed commits,
compose the control server and Docker daemon, wire environment/startup and README.
Run docs/acceptance.md scenarios including real model + tool, fake Telegram,
recursive CLI, recovery/connection interruption, PTY/64MiB output/files and skills.
Return concrete domain defects to owners. Finish all required checks, build the
installable package/container, document runnable commands and measured limitations.
No final completion claim is warranted until the complete product is verified.
