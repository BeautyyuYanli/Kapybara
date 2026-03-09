# Channel Design

## Data model

Structured input events use:

- `in_channel: str` (required)
- `contacts: list[str]` (optional, `<platform>/<user_id>` entries when known)
- `out_channel: str | None` (optional)

`out_channel=None` means "same as `in_channel`" for routing and skill selection.

Memory records keep:

- `in_channel: str` (required)
- `out_channel: str | None` (optional)

## Channel format

A channel is a URL-path-like hierarchy:

- Slash-separated segments
- No empty segments
- No leading or trailing slash

Example (Telegram thread input):

`telegram/chat/<chat_id>/thread/<message_thread_id>`

## Skill injection

Let `root(channel)` be the first channel segment.

- Messager skill: `messager/{root(effective_out_channel)}`
  - `effective_out_channel = out_channel or in_channel`

This keeps routing explicit while reusing platform-level skills.

## Memory retrieval

When retrieving memory for a channel prefix, filter by `MemoryRecord.in_channel` and `MemoryRecord.out_channel`
using prefix matching.

Example:

- Query prefix: `telegram/chat/<chat_id>`
- Matches:
  - `telegram/chat/<chat_id>`
  - `telegram/chat/<chat_id>/thread/1`
  - `telegram/chat/<chat_id>/thread/2`

## Preference injection

For an `in_channel`/`out_channel`, inject preferences in this order:

Preference files are resolved from `~/.kapybara/preferences`.

1. Root-level preference:
   - `PREFERENCES.md` if present
   - otherwise `PREFERENCES.default.md`
2. For each root-to-leaf channel prefix that exists, inject:
   - `<prefix>.md`
   - `<prefix>/PREFERENCES.md`

Example for `telegram/chat/<chat_id>`:

1. `PREFERENCES.md`
2. `telegram.md`
3. `telegram/PREFERENCES.md`
4. `telegram/chat.md`
5. `telegram/chat/PREFERENCES.md`
6. `telegram/chat/<chat_id>.md`
7. `telegram/chat/<chat_id>/PREFERENCES.md`

## Required Fields

Runtime input events require `in_channel`, with optional `contacts` and
`out_channel`. Memory retrieval requires records to carry `in_channel` (and
optional `out_channel`).
