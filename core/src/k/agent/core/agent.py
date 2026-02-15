"""Agent core wiring and runtime entrypoint.

This module owns:
- `MyDeps`: deps container shared by tools and runtime orchestration.
- `agent`: the `pydantic_ai.Agent` wiring (system prompts + tools).
- `agent_run`: the primary runtime entrypoint (memory selection + compaction).

Persona override:
    If `Config.fs_base / "PERSONA.md"` exists and is non-empty, its contents are
    used as the persona system prompt. Otherwise, if
    `Config.fs_base / "PERSONA.default.md"` exists and is non-empty, its
    contents are used. If neither file is present (or both are empty), no
    persona system prompt is added.
"""

from __future__ import annotations

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

from k.agent.core.entities import Event, MemoryHint, finish_action
from k.agent.core.media_tools import read_media
from k.agent.core.prompts import (
    SOP_prompt,
    bash_tool_prompt,
    general_prompt,
    input_event_prompt,
    intent_instruct_prompt,
    memory_instruct_prompt,
    response_instruct_prompt,
)
from k.agent.core.shell_tools import (
    bash,
    bash_input,
    bash_interrupt,
    bash_wait,
    edit_file,
)
from k.agent.core.skills_md import concat_skills_md, maybe_load_kind_skill_md
from k.agent.memory.compactor import run_compaction
from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config
from k.io_helpers.shell import ShellSessionManager
from k.runner_helpers.basic_os import BasicOSHelper


@dataclass()
class MyDeps:
    """Dependencies for the agent run.

    Lifecycle:
        `MyDeps` owns a `ShellSessionManager` which may keep subprocesses alive
        across multiple tool calls. Always close it when the deps are no longer
        needed (prefer `async with MyDeps(...)`).

    Input event:
        Some system prompts (e.g. skills selection) depend on the input event's
        `kind`. Populate `input_event_kind` for runs that provide a structured
        `Event` payload.

    Bash tool cadence:
        `count_down` is decremented once per bash-like tool call (tools that may
        return a `BashEvent`). When it reaches zero, the tool response appends a
        system message reminding the agent to post a progress update, then
        continue working.
    """

    config: Config
    memory_storage: FolderMemoryStore
    memory_parents: list[str]
    input_event_kind: str
    start_event: Event | None = None
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


async def fork(
    ctx: RunContext[MyDeps],
    instruct: str,
) -> str:
    """Run `instruct` in a forked agent run. The fork reuses the current conversation and memory context.

    Returns a short status string; on success it includes the forked run's
    compacted memory record.
    """

    parent_mems = []
    # parent_mems = (
    #     inject_memories if inject_memories else []
    # )
    message_history = copy(ctx.messages)
    if isinstance(message_history[-1], ModelResponse):
        if not ctx.tool_name or not ctx.tool_call_id:
            raise RuntimeError(
                "Tool name and call id must be set when forking from a ModelResponse"
            )
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
        _res, mem = await agent_run(
            model=ctx.model,
            config=ctx.deps.config,
            memory_store=ctx.deps.memory_storage,
            instruct=Event(
                kind=ctx.deps.input_event_kind,
                content="You are the forked agent to complete only the following instruct, ignoring the previous ones.\nInstruction: "
                + instruct,
            ),
            message_history=message_history,
            parent_memories=parent_mems,
        )
    except Exception as e:
        return f"Fork failed: {type(e).__name__}: {e}"
    else:
        mem.parents = list(set(mem.parents + ctx.deps.memory_parents))
        ctx.deps.memory_storage.append(mem)
        ctx.deps.memory_parents.append(mem.id_)
        return "\n".join(
            [
                "Fork succeeded.",
                f"- memory_id: {mem.id_}",
                "- record:",
                mem.dump_compated(),
            ]
        )


def _read_persona_override(fs_base: Path) -> str:
    """Load an optional persona override from `fs_base/PERSONA.md`.

    Returns:
        The override file contents (trimmed) when present and non-empty;
        otherwise an empty string (meaning "no persona override").

    Notes:
        This helper is intentionally forgiving: missing/unreadable files are
        treated as "no override" so agent runs don't fail due to configuration.
    """

    try:
        text = (fs_base / "PERSONA.md").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        try:
            text = (fs_base / "PERSONA.default.md").read_text(encoding="utf-8").strip()
        except FileNotFoundError, OSError:
            return ""
    except OSError:
        return ""
    return text or ""


agent = Agent(
    system_prompt=[
        bash_tool_prompt,
        input_event_prompt,
        response_instruct_prompt,
        memory_instruct_prompt,
        intent_instruct_prompt,
    ],
    tools=[bash, bash_input, bash_wait, bash_interrupt, edit_file, read_media, fork],
    deps_type=MyDeps,
    output_type=ToolOutput(finish_action, name="finish_action"),
)


@agent.system_prompt(dynamic=True)
def persona_prompt_from_fs(ctx: RunContext[MyDeps]) -> str:
    """Return the persona system prompt, preferring `fs_base/PERSONA.md`."""

    fs_base = Path(ctx.deps.config.fs_base)
    return _read_persona_override(fs_base)


@agent.system_prompt
def general_system_prompt() -> str:
    return general_prompt


@agent.system_prompt
def sop_system_prompt() -> str:
    return SOP_prompt


@agent.system_prompt
def concat_skills_prompt(ctx: RunContext[MyDeps]) -> str:
    base_path: str | Path = ctx.deps.config.fs_base
    skills_md = concat_skills_md(base_path)
    kind = ctx.deps.input_event_kind

    kind_chunks = [
        maybe_load_kind_skill_md(base_path, group="context", kind=kind),
        maybe_load_kind_skill_md(base_path, group="messager", kind=kind),
    ]
    kind_md = "\n".join(x for x in kind_chunks if x is not None).rstrip()

    if kind_md:
        return f"<BasicSkills>{skills_md}</BasicSkills>\n<KindSkills>{kind_md}\n</KindSkills>"
    return f"<BasicSkills>{skills_md}</BasicSkills>"


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


async def _memory_select(
    memory_store: FolderMemoryStore,
    parent_memories: list[str],
    compacted_level_num: int = 5,
    raw_pair_level_num: int = 20,
):
    recent_mem = set(parent_memories)
    all_mem = set(parent_memories)
    for mem in parent_memories:
        recent_mem.union(memory_store.get_ancestors(mem, level=compacted_level_num))
        all_mem.union(memory_store.get_ancestors(mem, level=raw_pair_level_num))

    all_mem_rec = memory_store.get_by_ids(all_mem)
    return all_mem_rec, recent_mem


async def agent_run(
    model: Model | KnownModelName,
    config: Config,
    memory_store: FolderMemoryStore,
    instruct: Event,
    message_history: Sequence[ModelMessage] | None = None,
    parent_memories: list[str] | None = None,
) -> tuple[str, MemoryRecord]:
    parent_memories = parent_memories or []

    all_mem_rec, recent_mem = await _memory_select(
        memory_store,
        parent_memories,
    )
    memory_string = "\n".join(
        x.dump_compated() if x.id_ in recent_mem else x.dump_raw_pair()
        for x in all_mem_rec
    )

    async with MyDeps(
        config=config,
        memory_storage=memory_store,
        memory_parents=parent_memories,
        input_event_kind=instruct.kind,
    ) as my_deps:
        res = await agent.run(
            model=model,
            deps=my_deps,
            user_prompt=(
                f"<Memory>{memory_string}</Memory>\n" if parent_memories else "",
                f"<System>Now: {datetime.now()}</System>\n",
                instruct.content,
            ),
            message_history=message_history,
        )
    msgs: list[ModelRequest | ModelResponse] = res.new_messages()
    msgs = _strip_history(msgs, (instruct.content,))
    compacted = await run_compaction(
        model=model,
        detailed=msgs,
    )
    output_hint = cast(MemoryHint, res.output)
    ref_mem = output_hint.referenced_memory_ids
    ref_mem = [
        mem_id for mem_id in ref_mem if memory_store.get_by_id(mem_id) is not None
    ]

    mem = MemoryRecord(
        input=instruct.content,
        compacted=compacted,
        output=output_hint.model_dump_json(exclude={"referenced_memory_ids"}),
        parents=list(set(parent_memories + ref_mem)),
        detailed=msgs,
        kind=instruct.kind,
    )
    return output_hint.model_dump_json(exclude={"referenced_memory_ids"}), mem


if __name__ == "__main__":
    import asyncio

    import logfire
    from pydantic_ai.models.openrouter import OpenRouterModel

    logfire.configure()
    logfire.instrument_pydantic_ai()

    async def main():
        config = Config(
            fs_base=Path("./data/fs"), basic_os_port=2222, basic_os_addr="localhost"
        )
        memory_store = FolderMemoryStore(config.fs_base / "memories")
        instruct = Event(
            kind="test",
            content="use `read_media` tool to read image and describe them to ~/image.txt : 1. https://fastly.picsum.photos/id/59/536/354.jpg?hmac=HQ1B2iVRsA2r75Mxt18dSuJa241-Wggf0VF9BxKQhPc \n 2. ./data/fs/961-536x354.jpg",
        )
        output, mem = await agent_run(
            model=OpenRouterModel("google/gemini-3-flash-preview"),
            config=config,
            memory_store=memory_store,
            instruct=instruct,
        )
        print("Agent output:", output)
        print("New memory record:", mem.dump_compated())

    asyncio.run(main())
