"""Temporary summarization and deterministic context assembly for the runner.

Only input/model nodes run during summarization: tool calls are answered with
format retries, never executed. The application-owned Agent configuration is
unchanged. Replay selection operates on original absolute message sequences;
database pagination must not change the selected window.
"""

from collections.abc import Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID

from pydantic_ai import Agent, CallToolsNode, ModelRequestNode, UserPromptNode
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from .types import Compaction

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

_FORMAT_RETRY = "Return a nonempty plain-text compaction summary. Do not call tools."


def require_nonnegative_int(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


async def summarize(
    agent: Agent[Any, Any],
    context: Sequence[ModelMessage],
    *,
    session_id: UUID,
    deps: Any,
    max_retries: int,
) -> str:
    """Return complete response text, closing the temporary graph on every exit.

    Each invalid complete response permits one retry up to max_retries. Incomplete
    responses fail immediately. Thinking is allowed but excluded from the summary.
    Native server tools and ordinary SDK initialization/hooks retain their original
    behavior; only client-side response handling and output validation are skipped.
    """
    async with agent.iter(
        COMPACTION_PROMPT,
        message_history=deepcopy(list(context)),
        conversation_id=str(session_id),
        deps=deps,
    ) as temporary:
        initial = temporary.next_node
        if not isinstance(initial, UserPromptNode):
            raise RuntimeError("Pydantic AI did not start at UserPromptNode")
        request = await temporary.next(initial)
        for attempt in range(max_retries + 1):
            if not isinstance(request, ModelRequestNode):
                raise RuntimeError("Expected a compaction ModelRequestNode")
            following = await temporary.next(request)
            if not isinstance(following, CallToolsNode):
                raise RuntimeError("Expected a complete compaction model response")
            response = following.model_response
            if response.state != "complete":
                raise UnexpectedModelBehavior("Compaction response is not complete")
            text = "".join(p.content for p in response.parts if isinstance(p, TextPart)).strip()
            if text and all(isinstance(p, (TextPart, ThinkingPart)) for p in response.parts):
                return text
            if attempt == max_retries:
                raise UnexpectedModelBehavior("Compaction did not return a plain-text summary")
            retries = [
                RetryPromptPart(
                    "The tool was not executed. " + _FORMAT_RETRY,
                    tool_name=part.tool_name,
                    tool_call_id=part.tool_call_id,
                )
                for part in response.parts
                if isinstance(part, ToolCallPart)
            ]
            request = ModelRequestNode(
                ModelRequest(parts=retries or [RetryPromptPart(_FORMAT_RETRY)])
            )
    raise AssertionError("Compaction retry loop must return or raise")


def replay_start(rows: Sequence[tuple[int, ModelMessage]], turns: int) -> int | None:
    """Find an absolute start in an ascending, contiguous suffix ending at the anchor.

    None requests an older page. The caller passes turns > 0. Include the request
    segment preceding the Nth response, then extend backwards until every local tool
    reply has its call. A page's extra prefix never becomes part of the window merely
    because it was fetched. Invalid history with an orphaned reply raises RuntimeError.
    """
    responses = [
        index for index, (_, message) in enumerate(rows) if isinstance(message, ModelResponse)
    ]
    if len(responses) < turns:
        return 0 if rows and rows[0][0] == 0 else None
    start = responses[-turns]
    while True:
        while start > 0 and isinstance(rows[start - 1][1], ModelRequest):
            start -= 1
        if start == 0 and rows[0][0] != 0:
            return None
        calls = {
            (part.tool_name, part.tool_call_id)
            for _, message in rows[start:]
            if isinstance(message, ModelResponse)
            for part in message.parts
            if isinstance(part, ToolCallPart)
        }
        replies = {
            (part.tool_name, part.tool_call_id)
            for _, message in rows[start:]
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_name is not None
        }
        missing = replies - calls
        if not missing:
            return rows[start][0]
        for index in range(start - 1, -1, -1):
            message = rows[index][1]
            if isinstance(message, ModelResponse):
                missing.difference_update(
                    (part.tool_name, part.tool_call_id)
                    for part in message.parts
                    if isinstance(part, ToolCallPart)
                )
            if not missing:
                start = index
                break
        else:
            if rows[0][0] == 0:
                raise RuntimeError("History contains a tool reply without its call")
            return None


def assemble_context(
    system_parts: Sequence[SystemPromptPart],
    compaction: Compaction,
    replay: Sequence[ModelMessage],
    tail: Sequence[ModelMessage],
) -> list[ModelMessage]:
    """Create virtual summary/replay/resume messages without mutating source history."""
    replay_copy = deepcopy(list(replay))
    for message in replay_copy:
        if isinstance(message, ModelRequest):
            message.parts = [p for p in message.parts if not isinstance(p, SystemPromptPart)]
    return [
        ModelRequest(
            parts=[
                *deepcopy(system_parts),
                UserPromptPart(COMPACTION_CONTEXT_PROMPT.format(summary_text=compaction.text)),
            ]
        ),
        *replay_copy,
        ModelRequest(parts=[UserPromptPart(COMPACTION_RESUME_PROMPT)]),
        *deepcopy(tail),
    ]
