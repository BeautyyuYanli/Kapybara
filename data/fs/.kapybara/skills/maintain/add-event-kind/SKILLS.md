---
name: add-event-kind
description: Guide and tools for adding a new platform channel root to Kapybara.
---

# add-event-kind

To add a new communication channel (e.g., Discord, Slack, etc.) to Kapybara,
you need to implement two platform-specific components: **Message Delivery**
and an **Event Starter**. Memory retrieval is provided by
`meta/retrieve-memory` (shared across platforms), not `context/<platform>`.

## 1. Memory retrieval (shared)
Use `~/.kapybara/skills/meta/retrieve-memory` for retrieval workflows.

- Recommended script: `~/.kapybara/skills/meta/retrieve-memory/stage_a`
- Input contract: pass the exact current `Event.in_channel` and keyword regex.
- Output contract: candidate memory rows (`id`, `core_json`,
  `matched_detailed_lines`) for follow-up inspection.

Preference content is injected by the agent system prompt pipeline, so retrieval
skills should return retrieval candidates only.

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
2. **Reuse** `meta/retrieve-memory` for memory lookup.
3. **Implement** `messager/<platform>` skill for replies.
4. **Create** `/core/src/k/starters/<platform>.py` to bridge the API to the Agent.
5. **Update** `~/start.sh` or a similar supervisor to run the new starter.
