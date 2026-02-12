from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse
from pydantic_ai.models import KnownModelName, Model


async def create_memory_record(
    compacted: list[str],
) -> list[str]:
    return compacted


compact_agent = Agent(
    system_prompt="""You are a memory compactor.

Goal
----
Convert the provided conversation/tool traces into a chronological list of concise, reusable steps.

Output format
-------------
- Return a list of strings (a JSON array of strings is also acceptable).
- Do not require any fixed per-line prefix, but each line must be unambiguous about who did what (user intent vs agent action).

What to keep (optimize for reuse)
--------------------------------
- Preserve the full task arc: request → investigation → execution → verification → result.
- Keep one major step per line; merge noisy sub-steps that share the same purpose.
- Prefer concrete, action-oriented phrasing: what was done, why it mattered, and the outcome.
- Keep details that help someone repeat the work later:
  - tool/skill names, key flags/options, file paths (e.g. `/tmp/...`), IDs (e.g. chat_id), chosen models/voices, extracted facts/results, and verification signals (e.g. "ok=true", "exit_code=0").
- Drop filler that doesn't affect decisions or outcomes (chit-chat, apologies, self-talk, repeated instructions).

Skills (special rule)
---------------------
- If the trace shows the agent reading or relying on a skill doc (`SKILLS.md`), include a short, task-relevant excerpted summary of the skill instructions and the skill path.
  - Summarize in one line per skill (do not paste the whole doc).
  - Keep only the parts that were relevant to the current task (what was actually used or needed), but include enough to reuse that subset: what it does, required inputs/env vars if mentioned, and the canonical command/API shape.
  - Example: `Reviewed ~/skills/misc/telegram/SKILLS.md: sendAudio/sendMessage via Bot API; needs TELEGRAM_BOT_TOKEN; requires chat_id + file path.`

Tool/command representation
---------------------------
- Keep commands readable and actionable. Keep full URLs (including paths/query strings) when they help trace the step; shorten truly huge non-URL payloads/outputs with "...".
- Do not include secrets or raw tokens. Redact them as `$ENV_VAR`, `<REDACTED>`, or "...", including when they appear inside a URL.
- Avoid dumping raw tool logs, stack traces, or large structured blobs; summarize the intent + result instead.
""",
    output_type=create_memory_record,
)


def print_detailed(detailed: list[ModelRequest | ModelResponse]):
    res = ""
    for msg in detailed:
        if isinstance(msg, ModelRequest):
            res += f"Inbound: {msg.parts!r}\n"
        else:
            res += f"Assistant: {msg.parts!r}\n"
    return res


async def run_compaction(
    model: Model | KnownModelName,
    detailed: list[ModelRequest | ModelResponse],
):
    compacted = await compact_agent.run(
        model=model,
        user_prompt=print_detailed(detailed),
    )
    return compacted.output


async def main():
    from rich import print

    with open("mem.jsonl", encoding="utf-8") as f:
        lines = f.read()

    res = await compact_agent.run(
        model="openai:gpt-5-chat-latest",
        user_prompt=lines,
    )
    print(res.output)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
