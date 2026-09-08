# Integrate session interaction protocol with provider management

Absorbed `feat/steer-queue-message-waiting-id` at `0344307eedea5130413173c62dedbb5720f76471`
into main `f4d0307185a552fdfd49a9b6156dc0eccfea22cc` on
`integrate/steer-queue-message-waiting-id`. No new delegation or feature redesign.

The branch's text/reply_to creation modes, per-input reply addresses, one-shot one-to-one
waiting channels and v2-only snapshots remain intact. The main branch's provider catalog,
registered model IDs, per-run backend resolution, budget overrides, private error handling,
plugin injection and history schema instructions are retained. Optional CLI --output-mode
preserves a config-file value when omitted. Legacy prompt conversion was removed consistently
with the incoming branch's explicit no-conversion contract.

Resolved ten conflicted files. Updated nullable receipt/JSON type assertions across tests and
scripts; conditional empty creation remains nullable. Removed the obsolete v1 prompt-upgrade
test and added six real-SDK output-function cases covering Chat, Responses and Google, including
history schema exposure and reply-address visibility. Known safe model-configuration failures
still reach receipts; unknown failures have null output and do not leak raw SDK messages.

Validation uses Docker UID/GID 10001, no capabilities, no privilege escalation, random PostgreSQL
schemas and Valkey namespaces on the Kapy bridge. Full merged run: 420 passed, one assertion
failure in 146.93s. That assertion expected omitted optional model fields instead of the existing
provider model's normalized null fields; only the test was corrected. Final complete affected
Gateway-control/model-protocol files: 24 passed in 2.93s. No production changes followed the full
run. Ruff check/format, whole-project Pyrefly (src/tests/scripts, zero errors), and diff checks
pass. Existing google-genai deprecation warning remains.

Deployment uses a new `kapy_interaction_v2` schema/namespace, because both State and Gateway
explicitly reject legacy business state. Existing `kapy_state` remains archived unchanged;
there is no v1 session conversion. Standalone providers/models/defaults, skills and saved
Telegram settings/poll offset are copied. Telegram route session bindings are cleared so the
next input creates a v2 session. Deployment verification and backup information follow below.

## Deployment and live acceptance

Integrated code commit: `7995d4f`. Built image manifest:
`sha256:6d5837c41bc9090ba01f794550b2df5175ab45ada54c12fbe410cc6615a3726a`.
Control and daemon were recreated; shared PostgreSQL, Valkey and network were not restarted.
Both processes run as UID 10001; daemon has zero capabilities and no-new-privileges enabled.
All 72 tracked source files match main in both deployed containers. Control is healthy,
daemon is running, and neither container logged a traceback.

Old schema dump (5,940,149 bytes), private prior environment and before/after table digests are
in `/tmp/kapy-interaction-deploy-txgu1fa9` (private directory; environment/dump mode 0600).
Rollback image: `kapy-v2:before-session-interaction`. `kapy_state` retains 11 sessions,
11,728 history records, 110 State requests and 91 Telegram inbox entries. Counts and content
digests for every old table matched after configuration copy and after live acceptance.
The archive also remains in the existing persistent PostgreSQL volume.

Copied one provider, all 67 models including observed metadata/revisions/user defaults, the
empty skills catalog, completed provider receipts, Telegram poll offset and saved route config.
Only route session bindings were cleared; the next message creates a new v2 session.
The local ignored .env now selects schema/namespace `kapy_interaction_v2`. Bot identity is
`@yanli_test1_bot`; no synthetic Telegram messages were sent.

Real-model acceptance used the unchanged configured Responses model with only test instructions
and random markers, never workspace/file/attachment content. The first recursive command was
rejected by automatic approval for an unspecified-payload concern; after inspecting its complete
payload and empty catalog, the exact command was approved and executed.

- `scripts/check_recursive.py`: real parent → non-root Docker CLI child → one completion event
  → parent resumption; generated marker matched, 19.82s. Temporary sessions were deleted.
- Explicit reply_to: empty creation returns null submission; repeated input uses the same
  address; full ReplyTo(kind, being_waited_ids, payload) reaches the receipt; repeated wait
  returns the same result without consumption. Real model passed in 5.07s; session deleted.
- Final running sessions, pending inputs, unhandled Telegram inbox, pending delivery and cleanup
  counts were all zero. No old protocol state was converted, deleted or replayed.

Rollback, if necessary, must first preserve any new-protocol activity: stop only control/daemon,
restore the previous local schema/namespace settings and rollback image, then restart those two
services. Do not overwrite either schema or replay the old poll offset after new updates without
reconciling Telegram ingress. This is an incompatible protocol cutover, not an in-place upgrade.
