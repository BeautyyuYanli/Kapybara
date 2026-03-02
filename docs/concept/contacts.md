# Contacts Design

## Overview

Contacts split identity into two layers:

- Platform contact id: `<platform>/<platform_id>` (for example, `telegram/567113516`)
- Internal unique contact id: short model-friendly id (`c[0-9a-z]+`, for example, `c1`, `c2`)

The agent receives platform ids in events and stores unique ids in memory.

## Contacts Book

The contacts registry lives at:

- `~/.kapybara/contacts.json`

Schema:

- `dict[str, list[str]]`
- key: unique contact id (`c[0-9a-z]+`)
- value: list of platform contact ids mapped to that unique id

Example:

```json
{
  "c1": ["telegram/567113516"],
  "c2": ["discord/alice"]
}
```

## Event vs Memory

- `Event.contacts`: list of platform contact ids (`<platform>/<platform_id>`)
- `MemoryRecord.contacts`: list of unique contact ids (`c[0-9a-z]+`)

At `agent_run` startup, event platform ids are resolved through `contacts.json`.
The resolved unique ids are then persisted to `MemoryRecord.contacts` when
`finish_action` returns the final record.

## Preference Files

User-level preference entrypoints remain platform-based:

- `~/.kapybara/preferences/contacts/<platform>/<platform_id>.md`

Canonical content is unique-id-based:

- `~/.kapybara/preferences/contacts/data/<unique_id>.md`

The platform paths are symlinks to data files. Prompt injection displays both
the symlink path and the resolved absolute target path when applicable.
