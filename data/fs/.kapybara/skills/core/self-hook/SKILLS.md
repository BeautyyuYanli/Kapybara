---
name: self-hook
description: Launches a detached follow-up `kapy` run that inherits the current run memory as parent context.
---

# self-hook

Use this skill to start a follow-up `kapy` run that is detached, shares the
same config base and memory graph, and waits for the current run's memory
record before it starts. Prefer the CLI's built-in `--async` mode so the CLI
itself allocates the logfile and prints the child pid/log path.

## Use this skill when

- You need a detached follow-up task.
- The child run must see the same on-disk config and memory tree as the parent.
- The child must not start until the current run's memory record exists.

## Read these values from the current prompt

- `Agent config base (...)` from the `<System>` section
- `Current run memory id` from the `<System>` section
- The active reply channel: `out_channel` if present, otherwise `in_channel`
- Every current contact, if any

## Rules

1. From an active run, always use `kapy --async` for detached follow-ups. Do
   not use the blocking pattern directly from an active run.
2. Set `K_CONFIG_BASE='<Agent config base path>'` explicitly so the child sees
   the same config, skills, and memories.
3. Pass the current run memory id as
   `--parent-memory='<Current run memory id>'`. Repeat `--parent-memory` for
   extra dependencies.
4. Set `--out-channel` to the active reply channel: `out_channel` if present,
   otherwise `in_channel`.
5. Omit `--in-channel` by default. Add
   `--in-channel='<meaningful-stable-channel>'` only when follow-ups should
   intentionally share channel-scoped history. Do not use `direct/default`.
6. Preserve contacts exactly: repeat `--contact='<contact>'` once per contact,
   or omit `--contact` if there are none.
7. Quote every argument as `--flag='value'` or `--flag="value"`.
8. `kapy --async` prints the detached child metadata as
   `pid=<pid> logfile=<path>`. Do not `wait` on that PID from the current run.

## Detached template

Use this from an active run.

```bash
env K_CONFIG_BASE='<Agent config base path from the <System> section of prompt>' \
  kapy --async \
  --out-channel='<current active out_channel: out_channel or in_channel>' \
  --parent-memory='<Current run memory id from the <System> section of prompt>' \
  "<follow-up prompt>"
```

## Blocking template

Use this only from external automation such as a shell script or cron job.

```bash
env K_CONFIG_BASE='<Agent config base path from the <System> section of prompt>' \
  kapy \
  --out-channel='<current active out_channel: out_channel or in_channel>' \
  --parent-memory='<Current run memory id from the <System> section of prompt>' \
  "<follow-up prompt>"
```

The caller is responsible for handling exit status, stdout, and stderr.

## Prompt-writing guidance

- Keep the follow-up prompt self-contained. State exactly what it should do and
  what output or side effects it should produce.
- Prefer one detached hook per clear objective instead of bundling unrelated
  work into a single background run.
- Set `K_CONFIG_BASE='<Agent config base path from the <System> section of prompt>'`
  explicitly instead of relying on inherited shell state.
- `kapy --async` allocates a fresh logfile automatically, so concurrent hooks
  do not overwrite each other.
