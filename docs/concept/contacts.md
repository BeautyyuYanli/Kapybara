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

When `finish_action` writes a memory record, it resolves event platform ids
through `contacts.json` and stores only the unique ids.

## Preference Files

User-level preference entrypoints remain platform-based:

- `~/.kapybara/preferences/contacts/<platform>/<platform_id>.md`

Canonical content is unique-id-based:

- `~/.kapybara/preferences/contacts/data/<unique_id>.md`

The platform paths are symlinks to data files. Prompt injection displays both
the symlink path and the resolved absolute target path when applicable.

## Migration

Run:

```bash
cd core
pdm run python scripts/migrate_contacts_preferences.py --config-base ~/.kapybara
```

Migration behavior:

- Ensures `contacts.json` exists
- Resolves/creates unique ids for existing platform preference files
- Creates missing `preferences/contacts/data/<unique_id>.md` files
- Rewrites `preferences/contacts/<platform>/<platform_id>.md` as relative symlinks
