# Channel Design

## Data model

Structured inputs and memories now use:

- `in_channel: str` (required)
- `out_channel: str | None` (optional)

`out_channel=None` means "same as `in_channel`" for routing and skill selection.

## Channel format

A channel is a URL-path-like hierarchy:

- Slash-separated segments
- No empty segments
- No leading or trailing slash

Example (Telegram thread input):

`telegram/chat/<chat_id>/thread/<message_thread_id>`

## Skill injection

Let `root(channel)` be the first channel segment.

- Context skill: `context/{root(in_channel)}`
- Messager skill: `messager/{root(effective_out_channel)}`
  - `effective_out_channel = out_channel or in_channel`

This keeps routing explicit while reusing platform-level skills.

## Memory retrieval

When retrieving memory for a channel prefix, filter by `MemoryRecord.in_channel`
using prefix matching.

Example:

- Query prefix: `telegram/chat/<chat_id>`
- Matches:
  - `telegram/chat/<chat_id>`
  - `telegram/chat/<chat_id>/thread/1`
  - `telegram/chat/<chat_id>/thread/2`

## Preference injection

For an `in_channel`, inject preferences from root to leaf. For each prefix path
that exists, inject both files in this order:

1. `<prefix>.md`
2. `<prefix>/PREFERENCES.md`

Example for `telegram/chat/<chat_id>`:

1. `telegram.md`
2. `telegram/PREFERENCES.md`
3. `telegram/chat.md`
4. `telegram/chat/PREFERENCES.md`
5. `telegram/chat/<chat_id>.md`
6. `telegram/chat/<chat_id>/PREFERENCES.md`

`by_user`-based preference filtering keeps the current behavior.

## Migration

### Why migrate from `kind`

`kind` is too flat for real routing. Messaging events usually need hierarchical
coordinates (platform -> chat -> thread -> message scope), and memory retrieval
often needs "this subtree" rather than one exact label.

The channel model replaces `kind` with path-like channels.

Folder memory stores with legacy `kind` fields must be migrated before loading
with the channel-only schema:

```bash
cd core
PYTHONPATH=src python3 -m k.agent.memory.folder_migrate_kind_to_channel --root ~/memories --apply
```

Run without `--apply` first for a dry-run report.
