---
name: self-hook
description: Launches a detached follow-up `kapy` run that inherits the current run memory as parent context.
---

# self-hook

Use this skill when you need to queue a follow-up `kapy` run without blocking
the current reply.

Rules:
- Always launch the follow-up with `nohup`.
- Always pass the **Current run memory id** from the `<System>` prompt as a
  `--parent-memory` value so the child run waits for this run's memory record
  to exist before starting.
- Always set the hook's `--out-channel` to the current run's active
  out-channel: use the current `out_channel` when it is set, otherwise use the
  current `in_channel`.
- Always generate a fresh hook input channel in the form `self-hook/<random-id>`.
- Always preserve the current run's contacts by repeating `--contact` once for
  each contact on the current run.
- If you need more than one parent, repeat `--parent-memory` once per id.
- Do not `wait` on the background PID in the current run; report the PID and
  log path instead.

Why this works:
- `kapy` waits for every `--parent-memory` id before it calls `agent_run`.
- Passing the current run memory id creates an explicit dependency edge from the
  detached run back to the memory produced by this run.

## Minimal pattern

```bash
CURRENT_MEMORY_ID="<Current run memory id from the <System> prompt>"
HOOK_OUT_CHANNEL="<current active out_channel: out_channel or in_channel>"
HOOK_IN_CHANNEL="self-hook/$(uuidgen | tr '[:upper:]' '[:lower:]')"
HOOK_LOG="/tmp/kapy_self_hook_$(date +%Y%m%d_%H%M%S).log"

nohup kapy \
  --in-channel "$HOOK_IN_CHANNEL" \
  --out-channel "$HOOK_OUT_CHANNEL" \
  --contact "<current_contact_1>" \
  --contact "<current_contact_2_if_any>" \
  --parent-memory "$CURRENT_MEMORY_ID" \
  "<follow-up prompt>" \
  >"$HOOK_LOG" 2>&1 < /dev/null &

printf 'pid=%s log=%s\n' "$!" "$HOOK_LOG"
```

## Prompt construction notes

- Keep the follow-up prompt self-contained: say exactly what the detached run
  should do and what output or side effects it should produce.
- Preserve all current-run contacts, and add extra repeated `--parent-memory`
  flags when you need more than the current run memory id.
- If the instruction is long, write it to a temp file first and then inject the
  fully quoted contents into the final `kapy` command.
- Prefer one detached hook per clear objective instead of bundling unrelated
  work into a single background run.
