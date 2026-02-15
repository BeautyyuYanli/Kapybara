---
name: context/telegram
description: Optimizes Telegram context/memory retrieval for speed and accuracy without building indexes.
---

# context/telegram

This skill is a lightweight, **no-index** workflow for retrieving relevant context from the local `~/memories/records` store when replying on Telegram.

The recommended entrypoint is the bundled **Stage A** script, which does a parallel candidate search by:
- `chat.id`
- `from.id`
- optional keyword regex (scoped to the same chat)

## Recommended: Stage A script

### Usage

```bash
~/skills/context/telegram/stage_a.sh \
  --chat-id <chat_id> \
  [--from-id <from_id>] \
  [--kw <regex>] \
  [--root <dir>] \
  [--n <N>] \
  [--out <dir>]
```

Notes:
- The script forces ripgrep output to **not** include line numbers.
- `--n` controls how many lines are kept per route (chat / user / kw). Default: `7`.

### Example

```bash
~/skills/context/telegram/stage_a.sh \
  --chat-id 567113516 \
  --from-id 567113516 \
  --kw 'retrieve-memory|neighborhood.py|skills/context/telegram' \
```

### Output

- Writes intermediate files into `--out` (default `/tmp/tg_ctx/`):
  - `by_chat.txt`, `by_user.txt`, `by_kw.txt`
- Prints a de-duped, path-sorted candidate list on stdout.

## Follow-ups (manual)

Once you have candidate record paths/IDs:
- Open a candidate `.core.json` to read the actual content.
- If you need higher-signal traces, search matching `.compacted.json`.
- If you need more surrounding context, expand via `~/skills/meta/retrieve-memory/neighborhood.py`.

