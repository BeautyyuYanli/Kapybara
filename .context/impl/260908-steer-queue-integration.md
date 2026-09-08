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
