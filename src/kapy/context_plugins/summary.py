"""Default summary/v1 page payload and upper-context rendering."""

from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from kapy.agent_runner.context import CallAgent, ContextInput, ContextPage, PageInput

# Original Codex prompt, with only the opening pause instruction added:
# https://github.com/openai/codex/blob/main/codex-rs/prompts/templates/compact/prompt.md
COMPACTION_PROMPT = """\
Pause your current task and follow the compaction instructions below for this response.

You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work.
"""  # noqa: E501

COMPACTION_CONTEXT_PROMPT = """\
Here is a summary of the conversation so far:

<compaction>
{summary_text}
</compaction>

Recent messages will be replayed below for context."""

COMPACTION_RESUME_PROMPT = (
    "The replay is complete. Continue the task, taking any subsequent messages into account. "
    "Do not repeat completed work."
)


class SummaryPlugin:
    key = "summary/v1"

    async def on_page(self, page: PageInput, *, call_agent: CallAgent) -> ContextPage:
        summary = await call_agent(COMPACTION_PROMPT, block_other_tools=True)
        return ContextPage(payload={"summary": summary})

    async def get_context(self, context: ContextInput) -> list[ModelMessage]:
        summary = context.page.payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary/v1 page requires a nonempty summary")
        return [
            ModelRequest(
                parts=[UserPromptPart(COMPACTION_CONTEXT_PROMPT.format(summary_text=summary))]
            ),
            *context.messages,
            ModelRequest(parts=[UserPromptPart(COMPACTION_RESUME_PROMPT)]),
        ]
