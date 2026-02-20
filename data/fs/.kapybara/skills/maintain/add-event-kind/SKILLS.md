---
name: add-event-kind
description: Guide and tools for adding a new event kind (communication channel) to Kapybara.
---

# add-event-kind

To add a new communication channel (e.g., Discord, Slack, etc.) to Kapybara, you need to implement three components: **Context Retrieval**, **Message Delivery**, and an **Event Starter**.

## 1. Context Retrieval Skill
Create a skill at `skills:context/<kind>/SKILLS.md` to define how to fetch history and preferences for that platform.

### Preference Management
The `context/<kind>` skill is responsible for retrieving preference files from `~/preferences/<kind>/`.

- **Global Preferences**: Content from `~/preferences/<kind>/preferences.md` is always loaded first. This provides the baseline persona and common rules for all interactions of this kind.
- **Fine-grained Preferences**: Content from `~/preferences/<kind>/by_chat/<chat_id>.md` (or similar) should be loaded if available, allowing per-chat customization.
- **Manual Update**: Users or agents can create or update preference information by directly editing the relevant file.

### Implementation Guide
- **Code**: The skill should include a script (e.g., `stage_a.sh`) that searches local memories by relevant IDs (e.g., `chat.id`, `from.id`).
- **Key Requirement**:
    - Output a list of candidate memory record paths sorted by time.
    - Prepend loaded preference contents to the output.

Example: `skills:context/telegram/stage_a` searches local memories and always loads `~/preferences/telegram/preferences.md`, plus chat-specific files if they exist.

## 2. Message Delivery Skill
Create a skill at `skills:messager/<kind>/SKILLS.md` to define how to reply via the platform's API (e.g., using `curl`).

Common methods to implement:
- `sendMessage`
- `sendPhoto`
- `setMessageReaction`

## 3. Event Starter
Implement a listener/polling script (usually in Python) that:
1. Polls the platform API for new updates.
2. Formats updates into an `Event(kind="<kind>", content=...)`.
3. Calls `k.agent.core.agent_run` with the Event.
4. Appends the resulting memory to the `FolderMemoryStore`.

Location: `/core/src/k/starters/<kind>.py`
Reference: `/core/src/k/starters/telegram.py`

## Workflow summary
1. **Identify** the Platform API (REST/WebSocket/Long-poll).
2. **Implement** `messager/<kind>` skill for replies.
3. **Implement** `context/<kind>` skill for memory lookup.
4. **Create** `/core/src/k/starters/<kind>.py` to bridge the API to the Agent.
5. **Update** `~/start.sh` or a similar supervisor to run the new starter.
