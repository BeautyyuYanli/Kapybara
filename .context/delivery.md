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
Real Telegram was enabled on 2026-09-08 at the user's explicit request. getMe,
getWebhookInfo (no webhook), the installed command menu and control HTTP checks
passed; docker-machine reconnected. The user subsequently confirmed real replies
with a screenshot. Old isolated acceptance stacks and their temporary volumes
were removed; normal development data volumes remain.

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

## Non-root deployment follow-up (2026-09-08)

User requires machines to have no root privileges. Architect-owned Dockerfiles now
default to kapy UID/GID10001. Compose daemon and dev machine explicitly select that
user, drop ALL capabilities, and set no-new-privileges. /run/kapy tmpfs has matching
ownership and mode0700; new volume roots are created with that owner in the image.
Existing kapy-v2_machine-data was migrated offline after confirming zero active
sessions; all four regular files retained identical content. Runtime checks passed
for both containers: all processes nonroot, all capability sets zero, setuid(0)
denied, application and venv unwritable. Full Compose suite262PASS116.92s, no skips;
JUnit .local/acceptance/nonroot.xml. Live check_system PASS7.54s, session deleted.
Fresh anonymous-volume/tmpfs ownership and ordinary-user writes also passed.
No domain code or dependencies changed; normal local stack runs the updated images.

## Telegram presentation follow-up (2026-09-08, delivered)

The user requests a natural chat frontend instead of separate delta/log messages.
New Gateway senior 94ba8801-e6f3-478b-a7f6-809ff1859b1e owns this follow-up from
main35016ec through proposal approval and cmd-impl. Proposal6c2494a was approved
with a simpler controlled, drained upgrade instead of a legacy log replay engine.
The architect must stop the old control process and verify no active runs,
unhandled ingress, pending fragments or unread output before replacing only the
legacy projection with the documented new empty shape, retaining its cursor.
If a concurrent input prevents draining, resume the old version and retry later.
Official sendMessageDraft
supports private-chat streaming previews; completed replies require sendMessage.
The architect owns integration and updating the running bot after review.
This new assignment does not reactivate any 20260907 delayed operations.

Final senior delivery18fd8a9 completed all five review stages and was merged.
Independent full-suite nonroot Docker run:270passed127.50s, zero skips.
Full-repository Ruff passed; Pyrefly zero errors (two suppressions,15 warnings).
After stopping old control, the architect rechecked zero active runs, pending
inputs, unhandled inbox and unread/pending output. Exactly one drained legacy
projection was converted to version1, retaining its cursor; the previous row was
saved privately under .local/acceptance. Sessions and history were not reset.
The new control is healthy, docker-machine reconnected, both still UID/GID10001.
Live production TelegramFrontend send_draft/send calls passed: two updates with
one stable draft ID, followed by one final update notification to the configured
user chat. This proves real Bot API draft/final transport; automated tests cover
State projection/recovery and ordering. No claim of exactly-once final delivery.

## Rich Markdown follow-up (2026-09-08, delivered)

User requests the latest Telegram Rich Messages rendering Markdown. The same new
Gateway senior94ba8801 prepared the proposal from main39bca06 on
feat/kapy-telegram-rich-markdown; this is a new authorized follow-up.
Official InputRichMessage.markdown and sendRichMessage/sendRichMessageDraft were
verified against the current API. Architect live preflight sent two rich drafts
with one stable ID and one final preview to the configured user chat. Both methods
succeeded; returned rich_message blocks were heading,paragraph,list,table,pre.
Proposal215b5b3 was approved and implementation1b97e019 reached review. Stage2
correctly rejected guessed English error descriptions. Architect then obtained
real content-limit rejection evidence from the configured Bot API. Each of the
following samples returned HTTP400, ok=false and error_code400 from both
sendRichMessageDraft and sendRichMessage; all requests were rejected, so no
oversized final messages were published:

| Raw Markdown sample | Exact description |
| --- | --- |
| 32769 ASCII x characters | Bad Request: RICH_MESSAGE_TEXT_TOO_LONG |
| 501 x paragraphs separated by two newlines | Bad Request: RICH_MESSAGE_BLOCKS_TOO_MANY |
| Markdown table with 21 columns | Bad Request: RICH_MESSAGE_TABLE_COLS_TOO_MANY |
| 17 nested block quotations (`> ` repeated17 then x) | Bad Request: RICH_MESSAGE_DEPTH_INVALID |

Only these verified descriptions may drive the new narrow content-rejection
fallback. The guessed English parsing/limit patterns must be removed; unknown400,
auth/rate/network/server errors remain on the existing failure/retry path.
Final delivery3e310927 completed all five review stages and was merged. Independent
full-suite nonroot Docker run:306passed130.44s, zero skips. Ruff passed and Pyrefly
reported zero errors (two suppressions,15 warnings). The rebuilt control image was
deployed with the existing configuration; no projection reset or migration was
needed. The existing daemon reconnected and both services still run UID/GID10001.
Live verification through the new production send_rich_draft/send_rich methods
succeeded: two updates sharing a draft ID and one final notification. Telegram
returned rich_message with heading,paragraph,list,pre blocks; prior native API
preflight also verified tables. Control HTTP200 and healthy startup confirmed.
