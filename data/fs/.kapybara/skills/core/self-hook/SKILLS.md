---
name: self-hook
description: Launches a detached follow-up `kapy` run that inherits the current run memory as parent context.
---

# self-hook

Use this skill to start a detached follow-up `kapy` run, shares the same config base and memory graph, and waits for the current run's memory record before it starts.


## Read these values from the current prompt

- `Agent config base (...)` from the `<System>` section
- `Current run memory id` from the `<System>` section
- The active reply channel: `out_channel` if present, otherwise `in_channel`
- Every current contact, if any

## Rules

1. From an active run, use the default detached `kapy` one-shot form. Do not add `--wait`.
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
7. Detached `kapy` prints the child metadata JSON to stdout as
   `{"pid":43210,"memory_id":"<reserved-memory-id>","logfile":"/path/to/kapy.log"}`.
8. Do not `wait` on the returned PID from the current run.


## Template

```bash
env K_CONFIG_BASE='<Agent config base path>' \
  kapy \
  --out-channel='<current active out_channel: out_channel or in_channel>' \
  --parent-memory='<Current run memory id>' \
  "<follow-up prompt>"
```

Example stdout:

```json
{"pid":43210,"memory_id":"<reserved-memory-id>","logfile":"/home/k/.kapybara/logs/kapy/kapy_20260309_120000_abcd1234.log"}
```
