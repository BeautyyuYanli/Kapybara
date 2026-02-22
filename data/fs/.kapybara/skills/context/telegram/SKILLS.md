---
name: context/telegram
description: Optimizes Telegram context/memory retrieval for speed and accuracy without building indexes.
---

# context/telegram

This skill is a lightweight, **no-index** workflow for retrieving relevant context from the local `~/memories/records` store when replying on Telegram.

The recommended entrypoint is the bundled **Stage A** script, which does a parallel candidate search by:
- `in_channel` prefix (required)
- `from.id`
- optional keyword regex (scoped to the same channel prefix)

Hard requirement:
- `--in-channel` must be the **exact `in_channel` of the current input**.
- If the current input is on a thread channel, pass the full thread channel path.
  Do not replace it with a broader parent prefix.

## Recommended: Stage A script

### Usage

```bash
~/.kapybara/skills/context/telegram/stage_a \
  --in-channel <exact_current_input_in_channel> \
  [--from-id <from_id>] \
  [--kw <regex>] \
  [--root <dir>] \
  [--n <N>] \
  --out <file>
```

Notes:
- `--in-channel` is not "any matching prefix"; provide the full current-input
  channel string as-is.
- Preference files are loaded from `~/.kapybara/preferences` using channel-prefix
  order plus the optional `by_user/<from_id>.md` file.
- The script records ripgrep match line numbers so you can jump to the exact `.detailed.jsonl` line.
- `--n` controls how many lines are kept per route (channel / user / kw). Default: `6`.
- Prefix matching is subtree-aware: `telegram/chat/<chat_id>` matches all
  records under that chat, including per-thread channels such as
  `telegram/chat/<chat_id>/thread/<message_thread_id>`.
- The `user` route is intentionally broader than `channel`: it searches
  by `from.id` across the same root (`telegram/*`), so thread messages can
  still recall that user's history outside the current thread.

### Example

```bash
~/.kapybara/skills/context/telegram/stage_a \
  --in-channel telegram/chat/567113516 \
  --from-id 567113516 \
  --kw 'retrieve-memory|telegram-context' \
  --out /tmp/tg_ctx_<unique>.tsv \
```

### Output

- Writes the full output to `--out` (required) and also prints it to stdout.
  - Use a unique `/tmp/tg_ctx_<unique>.tsv` to avoid races/clobbering.
  - If `--out` is missing or already exists, the script errors and tells you to pick a unique path.

Columns (TSV):
`id`, `routes`, `core_json`, `matched_detailed_lines`

Notes:
- `routes` is a comma-separated list of which routes matched (`channel`, `user`, `kw`).
- `matched_detailed_lines` is a JSON array of `{line, text}` objects; it is only non-empty when `--kw` is provided.

## Follow-ups (manual)

Once you have candidate record paths/IDs:
- Open a candidate `.core.json` to read metadata + `compacted` (one line).
- Open the sibling `.detailed.jsonl` to see the raw `input` (line 1), record
  `output` (line 2), and per-response tool calls (line 3+, one JSON array per line).
  This file can be verbose; prefer reading just the first few lines
  instead of loading the whole file.
