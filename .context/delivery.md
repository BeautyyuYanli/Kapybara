# Kapy v2 delivery ledger

The complete kapy_v2.md implementation is merged and accepted on 2026-09-08
(Asia/Singapore).
The lead owned architecture/scaffolding/environment, independent acceptance and
merges. Domain implementation was delegated through Lody worktree seniors.
Each senior used cmd-proposal with one Mei simplification review/pass, then
cmd-impl with persistent Elysia and independent Eden groups for all five stages.

## Reviewed deliveries

| Scope | Senior session | Final delivery | Main merge |
| --- | --- | --- | --- |
| State | 37703573-1ccd-4f8b-a137-ba52ca9e8e60 | ac18037 | acf69fc |
| Execution | 3312c3ad-b9ee-4150-928f-2bf7654b1aca | 52746c8 | cb895c0 |
| Execution PTY observation regression | same Execution senior | 4258267 | 28a6648 |
| Agent / Skills | a88a199c-41c1-49d6-ad09-2d5b87f80b12 | c334b42 | 12eba96 |
| Gateway / CLI / Telegram | 0f9cff83-dff7-4881-b5a1-a6b1e761dd27 | ac665c6 | 81b71ec |
| Integration closeout | 88b37983-bc47-40c1-88ff-2e9379136691 | 9515f9d | d892d21 |

Reports are retained under .context/impl. Domain code ends at d892d21; subsequent
lead changes correct test collection, acceptance harness usage and documentation.

## Final acceptance

- Standard Docker bridge, 2 GiB/256 PIDs/init, read-only code, isolated database data:
  262 passed in 110.74 s; zero failures/errors/skips. JUnit final-complete.xml under
  .local/acceptance includes all three docker_manager_acceptance.py tests.
- Ruff passes; pyrefly zero errors (two existing suppressions, 15 warnings).
- Real model -> installed kapy CLI -> child machine task -> one completion event ->
  resumed parent passed in 16.20 s. Temporary parent/child sessions deleted.
- Real control SIGKILL recovery passed: attempts 1/2, daemon stayed up and its
  command executed once. Prior system, State/event/output fanout, PTY, file and
  large-output checks have measured scopes in docs/acceptance-results.md.
- Wheel/build/uvx entry point and all 12 generated resource hashes passed.
- Telegram getMe/getChat passed. Fake Bot API tests cover the integration;
  no actual send, update consumption or bot setup was performed.

The earlier 259-test command passed a directory and an explicitly named file;
pytest omitted the nonstandard filename anyway. Root fixed shared python_files;
262 final distinct tests are the authoritative complete count.
The recursive harness initially invented an unauthorized waiting ID; it now uses
its actual CLI receipt channel. No Gateway change was needed.

## Running environment and boundaries

Normal Compose project kapy-v2 runs PostgreSQL17 (localhost55432), Valkey8
(localhost56379), stable network namespace, control (localhost8000) and Docker
execution daemon. API preview http://127.0.0.1:8000/docs was reported to Lody.
Real Telegram is explicitly disabled at startup. Old isolated acceptance stacks
and their temporary volumes were removed; normal development data volumes remain.

.env contains the corrected provider/TG configuration and generated local
control/signing/machine credentials, mode600 and ignored. Never print/commit it.
Use uv for lock/build and the corresponding generator for bundled apply_patch.
Machine/process/PTY/file research and tests run in Docker, never on the host.
Process groups/best-effort cleanup suffice; no cgroup/systemd work is authorized.
Compression uses API latest response input_tokens+output_tokens and one sweep per
fresh usage, never local counting. docs/contracts.md remains authoritative.

## Coordination history to avoid reviving stale work

Lody session_chat queued durable prompts instead of reliably steering live work.
After complete delivery, old queued messages led original Agent/Gateway owners to
revisit superseded scope. Both were cancelled/archived; their later branch heads
are not approved deliveries. Do not merge Agent 7f14cd0 or Gateway 242f4a7 or
resurrect cf2962f input-only. The actual shared-pool row-factory fix is in merged
Gateway de51640 and passed a single-connection regression. Earlier Execution
session515746bc was archived and replaced; do not revive its cgroup implementation.
The clean closeout senior completed the approved PATH/fixture/type corrections and
all five review stages. No implementation work remains assigned to the lead.
