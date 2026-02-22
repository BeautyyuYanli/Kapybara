---
name: add-event-kind
description: Guide and tools for adding a new platform channel root to Kapybara.
---

# add-event-kind

To add a new communication channel (e.g., Discord, Slack, etc.) to Kapybara, you need to implement three components: **Context Retrieval**, **Message Delivery**, and an **Event Starter**.

## 1. Context Retrieval Skill
Create a skill at `~/.kapybara/skills/context/<platform>/SKILLS.md` to define how to fetch history and preferences for that platform.

### Preference Management
The `context/<platform>` skill is responsible for retrieving preference files based on `Event.in_channel` prefixes.

- **Path Preferences**: Inject from `~/.kapybara/preferences`, in root-to-leaf order, both `<prefix>.md` and `<prefix>/PREFERENCES.md` for each `Event.in_channel` prefix.
- **Fine-grained Preferences**: Keep by-user files (e.g. `~/.kapybara/preferences/<platform>/by_user/<user_id>.md`) when available.
- **Manual Update**: Users or agents can create or update preference information by directly editing the relevant file.

### Implementation Guide
- **Code**: The skill should include a script (e.g., `stage_a`) that combines channel-prefix lookup (`MemoryRecord.in_channel`) with optional ID routes (e.g., `from.id`), where ID lookup may span the same platform root (for example, across `telegram/*`).
- **Key Requirement**:
    - Output a list of candidate memory record paths sorted by time.
    - Prepend loaded preference contents to the output.

Example: `~/.kapybara/skills/context/telegram/stage_a` searches local memories by `in_channel` prefix and loads matching channel-prefix preference files.

## 2. Message Delivery Skill
Create a skill at `~/.kapybara/skills/messager/<platform>/SKILLS.md` to define how to reply via the platform's API (e.g., using `curl`).

Common methods to implement:
- `sendMessage`
- `sendPhoto`
- `setMessageReaction`

## 3. Event Starter
Implement a listener/polling script (usually in Python) that:
1. Polls the platform API for new updates.
2. Formats updates into an `Event(in_channel="...", out_channel=None, content=...)`.
   - `in_channel` can be hierarchical, e.g. `telegram/chat/<chat_id>/thread/<thread_id>`.
   - `out_channel=None` means "same as input channel".
3. Calls `k.agent.core.agent_run` with the Event.
4. Appends the resulting memory to the `FolderMemoryStore`.

Location: `/core/src/k/starters/<platform>.py`
Reference: `/core/src/k/starters/telegram.py`

## Workflow summary
1. **Identify** the Platform API (REST/WebSocket/Long-poll).
2. **Implement** `messager/<platform>` skill for replies.
3. **Implement** `context/<platform>` skill for memory lookup.
4. **Create** `/core/src/k/starters/<platform>.py` to bridge the API to the Agent.
5. **Update** `~/start.sh` or a similar supervisor to run the new starter.
