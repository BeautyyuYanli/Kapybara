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
    UserPromptPart,
)

from .context import (
    ContextAssemblyContext,
    ContextPolicy,
    JsonObject,
    PageBoundary,
    PageTurnContext,
    close_tool_pairs,
)

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
        closed = close_tool_pairs(rows, start)
        if closed is None:
            return None
        if closed == start:
            return rows[start][0]
        start = closed


def summary_context_policy(
    agent: Agent[Any, Any],
    *,
    deps: Any = None,
    threshold_tokens: int | None = None,
    replay_turns: int = 10,
    max_retries: int = 2,
) -> ContextPolicy:
    """Construct the default summary/v1 strategy, borrowing Agent and deps.

    Automatic paging observes completed business usage; None disables its trigger.
    Manual paging still summarizes. Configuration is fixed for a runner lifetime.
    Auxiliary calls do not receive its per-run execution/output capabilities.
    """
    require_nonnegative_int(replay_turns, "replay_turns")
    require_nonnegative_int(max_retries, "max_retries")
    if threshold_tokens is not None:
        require_nonnegative_int(threshold_tokens, "threshold_tokens")
        if threshold_tokens == 0:
            raise ValueError("threshold_tokens must be positive")

    def should_turn(boundary: PageBoundary) -> bool:
        return (
            threshold_tokens is not None
            and boundary.latest_response_seq is not None
            and boundary.response_tokens is not None
            and sum(boundary.response_tokens) > threshold_tokens
            and (
                boundary.previous_anchor_seq is None
                or boundary.latest_response_seq > boundary.previous_anchor_seq
            )
        )

    async def on_turn(context: PageTurnContext) -> JsonObject:
        text = await summarize(
            agent,
            context.messages,
            session_id=context.session_id,
            deps=deps,
            max_retries=max_retries,
        )
        return {"summary": text}

    async def assemble(context: ContextAssemblyContext) -> list[ModelMessage]:
        if context.page is None:
            return [message for _, message in await context.read_history()]
        summary = context.page.payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary/v1 page requires a nonempty summary")
        rows: list[tuple[int, ModelMessage]] = []
        if replay_turns and context.prefix_through_seq >= 0:
            cursor = context.prefix_through_seq
            while True:
                chunk = await context.read_history_before(through_seq=cursor)
                if not chunk:
                    raise RuntimeError("Context page history is missing")
                rows[:0] = reversed(chunk)
                start = replay_start(rows, replay_turns)
                if start is not None:
                    rows = [(seq, msg) for seq, msg in rows if seq >= start]
                    break
                cursor = rows[0][0] - 1
        first = rows[:1] if rows and rows[0][0] == 0 else await context.read_history(through_seq=0)
        system = [
            part
            for _, message in first
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, SystemPromptPart)
        ]
        replay = deepcopy([message for _, message in rows])
        for message in replay:
            if isinstance(message, ModelRequest):
                message.parts = [p for p in message.parts if not isinstance(p, SystemPromptPart)]
        return [
            ModelRequest(
                parts=[
                    *deepcopy(system),
                    UserPromptPart(COMPACTION_CONTEXT_PROMPT.format(summary_text=summary)),
                ]
            ),
            *replay,
            ModelRequest(parts=[UserPromptPart(COMPACTION_RESUME_PROMPT)]),
        ]

    return ContextPolicy("summary/v1", should_turn, on_turn, assemble)
