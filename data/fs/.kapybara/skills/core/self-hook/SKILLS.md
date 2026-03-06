---
name: self-hook
description: Launches a detached follow-up `kapy` run that inherits the current run memory as parent context.
---

# self-hook

Use this skill when you need to start a follow-up `kapy` run that inherits the
current run's config base, memory dependency, and routing context.

Use the non-blocking style when launching from the current run. A blocking
style is included later as a reference for scripts or cronjobs that are allowed
to wait for completion.

## Required behavior

1. When launching from the current run, use the non-blocking `nohup` style.
   Do not use the blocking style directly from an active run because it would
   block the current reply.
2. Preserve the current runtime config base.
   Read the concrete `Agent config base (...)` value from the `<System>` prompt
   and pass it with `--config-base` so the child sees the same memories,
   skills, and preferences tree as the current run.
3. Pass the **Current run memory id** from the `<System>` prompt as
   `--parent-memory`.
   This makes the child wait until this run's memory record exists before it
   starts.
4. Set `--out-channel` to the current run's active output channel.
   Use the current `out_channel` when it is set. Otherwise use the current
   `in_channel`.
5. Default `--in-channel` to `self-hook/default`.
   Only switch to `self-hook/<meaningful-unique-slug>` when you explicitly need
   channel isolation, such as concurrent hooks that should not share
   channel-scoped retrieval or history. Do not use random ids; the slug should
   describe the follow-up task.
6. Preserve the current run's contacts by repeating `--contact` once per
   contact.
   If the current run has no contacts, omit `--contact` entirely.
7. If you need more than one dependency, repeat `--parent-memory` once per id.
8. Do not `wait` on the background PID in the current run.
   Report the PID and log path instead.

## Why these rules matter

- `kapy` waits for every `--parent-memory` id before it calls `agent_run`.
- Passing the current run memory id creates an explicit dependency edge from
  the detached run to the memory produced by this run.
- If the parent run never writes that reserved memory record, the child timing
  out is intentional. It prevents the follow-up from running against missing or
  incomplete parent context.
- Reusing the same `--config-base` keeps the child on the same on-disk memory
  and skills graph, so the parent-memory wait loop can see the record it
  depends on.

## Non-blocking pattern (direct use)

```bash
HOOK_LOG="/tmp/kapy_self_hook_$(date +%Y%m%d_%H%M%S).log"

nohup kapy \
  --config-base='<Agent config base path from the <System> prompt>' \
  --in-channel='self-hook/default' \
  --out-channel='<current active out_channel: out_channel or in_channel>' \
  --contact='<current_contact_1>' \
  --contact='<current_contact_2_if_any>' \
  --parent-memory='<Current run memory id from the <System> prompt>' \
  "$(cat <<'EOF'
<follow-up prompt>
EOF
)" \
  >"$HOOK_LOG" 2>&1 < /dev/null &

printf 'pid=%s log=%s\n' "$!" "$HOOK_LOG"
```

Use this pattern when calling the hook from an active run.

## Blocking pattern (scripts or cronjobs only)

Use this only when the caller is external automation that is expected to wait,
such as a shell script or cronjob. Do not call this pattern directly from an
active run.

```bash
kapy \
  --config-base='<Agent config base path from the <System> prompt>' \
  --in-channel='self-hook/default' \
  --out-channel='<current active out_channel: out_channel or in_channel>' \
  --contact='<current_contact_1>' \
  --contact='<current_contact_2_if_any>' \
  --parent-memory='<Current run memory id from the <System> prompt>' \
  "$(cat <<'EOF'
<follow-up prompt>
EOF
)"
```

The caller is responsible for handling exit status, stdout, and stderr.

## Authoring tips

- Keep the follow-up prompt self-contained. State exactly what the detached run
  should do and what output or side effects it should produce.
- Prefer `--flag='value'` or `--flag='<placeholder>'` for literal arguments to
  reduce shell-escaping mistakes.
- Prefer an inline single-quoted here-doc (`$(cat <<'EOF' ... EOF)`) for the
  prompt body. This keeps `$VAR`, backticks, and backslashes literal inside the
  follow-up instruction.
- Prefer a timestamped log filename so concurrent hooks do not overwrite the
  same `/tmp` log file.
- Prefer one detached hook per clear objective instead of bundling unrelated
  work into a single background run.
