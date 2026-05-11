"""Agent core wiring and runtime entrypoint.

This module owns:
- `MyDeps`: deps container shared by tools and runtime orchestration.
- `agent`: the `pydantic_ai.Agent` wiring (system prompts + tools).
- `agent_run`: the primary runtime entrypoint (memory selection + compaction).

Preference injection:
    Channel preferences are injected from
    `<config_base>/preferences` (`Config.config_base`) using root-to-leaf
    `Event.in_channel` prefixes plus the effective output-channel prefixes
    (`Event.out_channel` or fallback to `Event.in_channel`), following
    `docs/concept/channel.md`.
    A root-level preference is injected first (`PREFERENCES.md` when present;
    otherwise `PREFERENCES.default.md`). Then for each routed channel prefix
    inject `<prefix>.md` and `<prefix>/PREFERENCES.md`, deduping repeated
    paths.
    When `Event.contacts` includes `<platform>/<user_id>` ids, also inject
    `contacts/<platform>/<user_id>.md` for each id.
    The loaded preference content is injected as a system prompt before skills
    are concatenated.

Memory compaction contract:
    The canonical high-fidelity `compacted_actions` prompt lives in
    `k.agent.core.prompts.compacted_prompt`

Memory retrieval boundaries:
    Memory selection + persistence lifecycle lives in
    `k.agent.core.memory_injection`.
    Preference discovery lives in `k.agent.core.preference_injection`.
    Runtime prompt builders live in `k.agent.core.runtime_prompting`.
    This module only wires those collaborators into the runtime entrypoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from pydantic_ai import (
    Agent,
    ModelMessage,
    RunContext,
    ToolOutput,
)
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.models import KnownModelName, Model
from pydantic_ai.usage import UsageLimits
from shell_session_manager import ShellSessionManager

from k.agent.core.entities import Event, tool_exception_guard
from k.agent.core.media_tools import read_media
from k.agent.core.memory_injection import (
    MemoryRunState,
    build_finish_action_record,
    finalize_memory_record,
    prepare_memory_run_state,
)
from k.agent.core.preference_injection import load_preferences_prompt
from k.agent.core.prompts import (
    SOP_prompt,
    bash_tool_prompt,
    compacted_prompt,
    general_prompt,
    input_event_prompt,
    intent_instruct_prompt,
    memory_instruct_prompt,
    preference_prompt,
    response_instruct_prompt,
)
from k.agent.core.runtime_prompting import event_meta_prompt, system_runtime_prompt
from k.agent.core.shell_tools import (
    bash,
    bash_input,
    bash_interrupt,
    bash_wait,
    edit_file,
)
from k.agent.core.skills_md import concat_skills_md, maybe_load_channel_skill_md
from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore
from k.agent.memory.paths import memory_root_from_config_base
from k.config import Config
from k.runner_helpers.basic_os import BasicOSHelper


@dataclass(slots=True)
class MyDeps:
    """Dependencies for the agent run.

    Lifecycle:
        `MyDeps` owns a `ShellSessionManager` which may keep subprocesses alive
        across multiple tool calls. Always close it when the deps are no longer
        needed (prefer `async with MyDeps(...)`).

    Input event:
        Prompt builders use
        `start_event.in_channel` / `start_event.out_channel` as the canonical
        routing source. `start_event.contacts` is optional user identity context
        for contact-scoped preference injection. System prompts use channels and
        contacts for preference + skill injection.
        `memory_run` groups the prepared memory prompt, inherited parent ids,
        resolved contact ids, and reserved output-record timestamp for this run.
        Always provide `start_event` for agent runs.

    Bash tool cadence:
        `count_down` is decremented once per bash-like tool call (tools that may
        return a `BashEvent`). When it reaches zero, the tool response appends a
        system message reminding the agent to post a progress update, then
        continue working.

    """

    config: Config
    memory_storage: FolderMemoryStore
    memory_run: MemoryRunState
    start_event: Event
    bash_cmd_history: list[str] = field(default_factory=list)
    count_down: int = 6
    stuck_warning: int = 0
    stuck_warning_limit: int = 3
    basic_os_helper: BasicOSHelper = field(init=False)
    shell_manager: ShellSessionManager = field(init=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        self.basic_os_helper = BasicOSHelper(config=self.config)
        self.shell_manager = ShellSessionManager()

    async def __aenter__(self) -> MyDeps:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close resources owned by these deps (idempotent)."""

        if self._closed:
            return
        self._closed = True
        await self.shell_manager.close()


@tool_exception_guard
async def fork(
    ctx: RunContext[MyDeps],
    instruct: str,
) -> str:
    """Run `instruct` in a forked agent run.

    The fork reuses the current conversation and memory context by copying the
    current model history and appending a synthetic tool-return message that
    represents the current tool call completion.

    Returns a short status string; on success it includes the forked run's
    compacted memory record.

    Notes:
        This helper is currently kept for reference and potential re-enable.
        It is intentionally not registered in `agent.tools` below.
    """

    parent_mems = []
    # parent_mems = (
    #     inject_memories if inject_memories else []
    # )
    # Copy the current exchange so the child run starts from the same context.
    message_history = copy(ctx.messages)
    if isinstance(message_history[-1], ModelResponse):
        if not ctx.tool_name or not ctx.tool_call_id:
            raise RuntimeError(
                "Tool name and call id must be set when forking from a ModelResponse"
            )
        # The child run should see this tool call as completed before continuing.
        message_history.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name=ctx.tool_name,
                        content="Success. You are continuing as the forked agent.",
                        tool_call_id=ctx.tool_call_id,
                    )
                ]
            )
        )
    else:
        raise RuntimeError("Last message when forking must be a ModelResponse")

    try:
        # Run the delegated instruction as a normal agent run with inherited history.
        mem = await agent_run(
            model=ctx.model,
            config=ctx.deps.config,
            memory_store=ctx.deps.memory_storage,
            instruct=Event(
                in_channel=ctx.deps.start_event.in_channel,
                contacts=ctx.deps.start_event.contacts,
                out_channel=ctx.deps.start_event.out_channel,
                content="You are the forked agent to complete only the following instruct, ignoring the previous ones.\nInstruction: "
                + instruct,
            ),
            message_history=message_history,
            parent_memories=parent_mems,
        )
    except Exception as e:
        return f"Fork failed: {type(e).__name__}: {e}"
    else:
        mem.parents = list(set(mem.parents + ctx.deps.memory_run.memory_parents))
        ctx.deps.memory_storage.append(mem)
        ctx.deps.memory_run.memory_parents.append(mem.id_)
        return "\n".join(
            [
                "Fork succeeded.",
                f"- memory_id: {mem.id_}",
                "- record:",
                mem.dump_compated(),
            ]
        )


def finish_action(
    ctx: RunContext[MyDeps],
    referenced_memory_ids: list[str],
    raw_input: str,
    raw_output: str,
    input_intents: str,
    compacted_actions: list[str],
) -> MemoryRecord:
    """Finalize the run with a structured summary.

    Call this as the final step after all required channel responses are sent.
    The payload should summarize what the user asked and how you reacted.
    Detailed field contracts are defined in `<CompactedRules>`.

    Args:
        referenced_memory_ids: See `<CompactedRules>` field contract for `referenced_memory_ids`.
        raw_input: See `<CompactedRules>` field contract for `raw_input`.
        raw_output: See `<CompactedRules>` field contract for `raw_output`.
        input_intents: See `<CompactedRules>` field contract for
            `input_intents`.
        compacted_actions: Distilled process log of the whole task.
            Provide chronological, high-fidelity step lines following
            `<CompactedRules>`.

    Contract:
        `ctx.deps.memory_run.working_memory_created_at` reserves the final record
        timestamp before model execution in `agent_run`; this tool must
        preserve that `created_at` so the `<System>` prompt and persisted
        memory record agree on the derived memory id.
    """

    return build_finish_action_record(
        memory_store=ctx.deps.memory_storage,
        memory_run=ctx.deps.memory_run,
        start_event=ctx.deps.start_event,
        referenced_memory_ids=referenced_memory_ids,
        raw_input=raw_input,
        raw_output=raw_output,
        input_intents=input_intents,
        compacted_actions=compacted_actions,
    )


agent = cast(
    Agent[MyDeps, MemoryRecord],
    Agent(
        system_prompt=[],
        tools=[
            bash,
            bash_input,
            bash_wait,
            bash_interrupt,
            edit_file,
            read_media,
            # `fork` is intentionally disabled for now.
            # fork,
        ],
        deps_type=MyDeps,
        output_type=ToolOutput(finish_action, name="finish_action"),
    ),
)


agent.system_prompt(lambda: general_prompt)
agent.system_prompt(lambda: bash_tool_prompt)
agent.system_prompt(lambda: input_event_prompt)
agent.system_prompt(lambda: response_instruct_prompt)
agent.system_prompt(lambda: memory_instruct_prompt)
agent.system_prompt(lambda: preference_prompt)
agent.system_prompt(lambda: intent_instruct_prompt)
agent.system_prompt(lambda: compacted_prompt)


@agent.system_prompt
def preferences_system_prompt(ctx: RunContext[MyDeps]) -> str:
    """Inject routed channel-scoped preferences ahead of skill documents.

    Registration order matters: this function is intentionally declared before
    `concat_skills_prompt` so preference guidance appears first.
    """

    return load_preferences_prompt(
        in_channel=ctx.deps.start_event.in_channel,
        contacts=ctx.deps.start_event.contacts,
        pref_root=ctx.deps.config.config_base / "preferences",
        out_channel=ctx.deps.start_event.out_channel,
    )


@agent.system_prompt
def injected_memories_system_prompt(ctx: RunContext[MyDeps]) -> str:
    """Inject run-scoped memory context computed once in `agent_run`."""

    return ctx.deps.memory_run.injected_memories_prompt


@agent.system_prompt
def concat_skills_prompt(ctx: RunContext[MyDeps]) -> str:
    config_base: str | Path = ctx.deps.config.config_base
    skills_md = concat_skills_md(config_base)
    event = ctx.deps.start_event
    out_channel = event.effective_out_channel

    channel_chunks = [
        maybe_load_channel_skill_md(
            config_base,
            group="messager",
            channel=out_channel,
        ),
    ]
    channel_md = "\n".join(x for x in channel_chunks if x is not None).rstrip()

    if channel_md:
        return f"<BasicSkills>{skills_md}</BasicSkills>\n<ChannelSkills>{channel_md}\n</ChannelSkills>"
    return f"<BasicSkills>{skills_md}</BasicSkills>"


@agent.system_prompt
def sop_system_prompt() -> str:
    return SOP_prompt


def _strip_history(
    msgs: list[ModelRequest | ModelResponse], instruct: Sequence[UserContent]
):
    first_msg = msgs[0]
    if isinstance(first_msg, ModelRequest):
        last_part = first_msg.parts[-1]
        if isinstance(last_part, UserPromptPart):
            last_part = copy(last_part)
            last_part.content = instruct  # update the first message's instruct part to the current instruct
        first_msg = copy(first_msg)
        first_msg.parts = [last_part]  # only keep the instruct
    msgs = [
        first_msg,
        *msgs[1:-1],
    ]  # remove initial message and final finish message
    return msgs


async def agent_run(
    model: Model | KnownModelName,
    config: Config,
    memory_store: FolderMemoryStore,
    instruct: Event,
    message_history: Sequence[ModelMessage] | None = None,
    parent_memories: list[str] | None = None,
    working_memory_created_at: datetime | None = None,
) -> MemoryRecord:
    """Run the agent with memory + event context and persistable output.

    User prompt order (fixed):
    1. `<System>Now + working-memory id derived from reserved created_at + agent config-base runtime view</System>`
    2. `<EventMeta>...</EventMeta>`
    3. real instruction content (`Event.content`)

    Memory injection:
    - The optional `<Memories>...</Memories>` block is injected as a
      non-dynamic system prompt.
    - Selection and serialization happen once inside `agent_run` before
      entering `agent.run`.
    - After the run completes, caller-supplied explicit parent ids are merged
      back into the final `MemoryRecord.parents`.

    Contact lifecycle:
    - Resolve `Event.contacts` platform ids to unique ids before model run.
    - Persist those resolved ids in the output `MemoryRecord.contacts`.

    Working-memory lifecycle:
    - Reserve the final `MemoryRecord.created_at` before entering `agent.run`.
    - Expose the derived memory id through `<System>`.
    - Preserve the same `created_at` in `finish_action`.
    - Callers may pass `working_memory_created_at` to preserve a previously
      reserved id across processes; otherwise `agent_run` reserves one locally.

    Parent-memory selection:
    - `parent_memories` provided and non-empty: treat them as explicit root
      memory ids, keep every explicit root injected as compacted, and merge the
      resulting injections with the auto-generated scope injections below.
      When the explicit roots already fill the compacted cap, stop expanding
      their ancestors instead of truncating those roots.
      Those explicit roots are also forced back into the final persisted
      `referenced_memory_ids`.
    - `parent_memories` omitted or empty: inject only the auto-generated
      MemoryStore context:
      1) latest 5 records whose `in_channel` or effective `out_channel`
         routes through the requested `in_channel` subtree, then expand with
         1-level compacted ancestors + 3-level raw-pair-only ancestors.
      2) latest 1 record per resolved contact id, expand with 1-level
         raw-pair-only ancestors.
      Caps:
      - channel scope: compacted 15, raw-pair 15
      - contact scope: compacted 5, raw-pair 5

    Request limits:
    - Uses default pydantic-ai `UsageLimits()` for request counting
      (`request_limit=50` unless overridden upstream).
    - The active request limit is also mirrored to run metadata so bash tools
      can emit near-limit warnings in `BashEvent.system_msg`.
    """

    memory_run = prepare_memory_run_state(
        memory_store,
        config_base=config.config_base,
        instruct=instruct,
        parent_memories=parent_memories,
        working_memory_created_at=working_memory_created_at,
    )

    usage_limits = UsageLimits()

    async with MyDeps(
        config=config,
        memory_storage=memory_store,
        memory_run=memory_run,
        start_event=instruct,
    ) as my_deps:
        res = await agent.run(
            model=model,
            deps=my_deps,
            user_prompt=(
                await system_runtime_prompt(
                    basic_os_helper=my_deps.basic_os_helper,
                    shell_manager=my_deps.shell_manager,
                    working_memory_created_at=my_deps.memory_run.working_memory_created_at,
                ),
                event_meta_prompt(instruct),
                instruct.content,
            ),
            message_history=message_history,
            usage_limits=usage_limits,
            metadata={
                # `RunContext` doesn't expose UsageLimits directly; make the
                # active request cap available to dynamic system prompts.
                "request_limit": usage_limits.request_limit,
            },
        )
    msgs: list[ModelRequest | ModelResponse] = res.new_messages()
    msgs = _strip_history(msgs, (instruct.content,))
    return finalize_memory_record(
        memory_store=memory_store,
        memory_run=memory_run,
        memory_record=res.output,
        instruct_content=instruct.content,
        detailed_messages=msgs,
    )


if __name__ == "__main__":
    import asyncio

    import logfire
    from pydantic_ai.models.openrouter import OpenRouterModel

    logfire.configure()
    logfire.instrument_pydantic_ai()

    async def main():
        config = Config(
            config_base=Path("./data/fs/.kapybara"),
            ssh_port=2222,
            ssh_addr="localhost",
            ssh_user="k",
        )
        memory_store = FolderMemoryStore(
            memory_root_from_config_base(config.config_base)
        )
        instruct = Event(
            in_channel="test",
            contacts=["test/system"],
            content="use `read_media` tool to read image and describe them to ~/image.txt : 1. https://fastly.picsum.photos/id/59/536/354.jpg?hmac=HQ1B2iVRsA2r75Mxt18dSuJa241-Wggf0VF9BxKQhPc \n 2. ./data/fs/961-536x354.jpg",
        )
        mem = await agent_run(
            model=OpenRouterModel("google/gemini-3-flash-preview"),
            config=config,
            memory_store=memory_store,
            instruct=instruct,
        )
        print("Agent output:", mem.dump_compated())

    asyncio.run(main())
