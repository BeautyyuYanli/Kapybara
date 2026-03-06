"""Agent core wiring and runtime entrypoint.

This module owns:
- `MyDeps`: deps container shared by tools and runtime orchestration.
- `agent`: the `pydantic_ai.Agent` wiring (system prompts + tools).
- `agent_run`: the primary runtime entrypoint (memory selection + compaction).

Preference injection:
    Channel preferences are injected from
    `<config_base>/preferences` (`Config.config_base`) using root-to-leaf
    `Event.in_channel` prefixes, following `docs/concept/channel.md`.
    A root-level preference is injected first (`PREFERENCES.md` when present;
    otherwise `PREFERENCES.default.md`). Then for each channel prefix inject
    `<prefix>.md` and `<prefix>/PREFERENCES.md`.
    When `Event.contacts` includes `<platform>/<user_id>` ids, also inject
    `contacts/<platform>/<user_id>.md` for each id.
    The loaded preference content is injected as a system prompt before skills
    are concatenated.

Memory compaction contract:
    The canonical high-fidelity `compacted_actions` prompt lives in
    `k.agent.core.prompts.compacted_prompt`

Memory retrieval boundaries:
    Scope-specific auto-retrieval lives in
    `k.agent.memory.retrieval.by_in_channel` and
    `k.agent.memory.retrieval.by_contact`. Caller-supplied parent-memory ids
    are resolved through `k.agent.memory.utils.get_memory_ids_from_roots`.
    This module only merges those injected-memory sources into one
    `<Memories>` block.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from logging import getLogger
from pathlib import Path
from typing import cast

from pydantic_ai import (
    Agent,
    ModelMessage,
    ModelRetry,
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

from k.agent.channels import iter_channel_prefixes
from k.agent.contacts import contact_preference_path, resolve_contact_unique_ids
from k.agent.core.entities import Event, tool_exception_guard
from k.agent.core.media_tools import read_media
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
from k.agent.core.shell_tools import (
    bash,
    bash_input,
    bash_interrupt,
    bash_wait,
    edit_file,
)
from k.agent.core.skills_md import concat_skills_md, maybe_load_channel_skill_md
from k.agent.memory.entities import (
    MemoryRecord,
    is_memory_record_id,
    memory_record_id_from_created_at,
)
from k.agent.memory.folder import FolderMemoryStore
from k.agent.memory.paths import memory_root_from_config_base
from k.agent.memory.retrieval.by_contact import (
    latest_memory_roots_by_contact,
    select_memory_ids_by_contact,
)
from k.agent.memory.retrieval.by_in_channel import (
    latest_memory_roots_by_in_channel,
    select_memory_ids_by_in_channel,
)
from k.agent.memory.store import MemoryStore
from k.agent.memory.utils import dedupe_memory_ids, get_memory_ids_from_roots
from k.config import Config
from k.io_helpers.shell import ShellSessionManager
from k.runner_helpers.basic_os import (
    AGENT_CONFIG_BASE_EXPR,
    BasicOSHelper,
    agent_config_base_value,
)

logger = getLogger(__name__)


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
        `memory_parents` stores the deduped root ids whose context was injected
        for this run so forked runs can inherit graph parents without reusing
        serialized prompt blocks.
        `working_memory_created_at` reserves the final `MemoryRecord`
        timestamp at the start of `agent_run`. Dynamic system prompts derive
        the pending memory id from that timestamp, and `finish_action` must
        preserve the same `created_at` in the final persisted record.
        `injected_memories_prompt` stores the `<Memories>` block prepared once
        in `agent_run` and injected via a non-dynamic system prompt.
        `resolved_contact_ids` is the unique-id view of `start_event.contacts`,
        resolved before model execution.
        Always provide `start_event` for agent runs.

    Bash tool cadence:
        `count_down` is decremented once per bash-like tool call (tools that may
        return a `BashEvent`). When it reaches zero, the tool response appends a
        system message reminding the agent to post a progress update, then
        continue working.

    """

    config: Config
    memory_storage: FolderMemoryStore
    memory_parents: list[str]
    start_event: Event
    working_memory_created_at: datetime
    resolved_contact_ids: list[str]
    injected_memories_prompt: str = ""
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


def _root_preference_candidates(pref_root: Path) -> list[Path]:
    """Build root-level preference candidate file paths in priority order.

    If `PREFERENCES.md` exists, do not include `PREFERENCES.default.md`.
    """

    preferred = pref_root / "PREFERENCES.md"
    default = pref_root / "PREFERENCES.default.md"
    if preferred.exists():
        return [preferred]
    return [default]


def _channel_preference_candidates(in_channel: str, *, pref_root: Path) -> list[Path]:
    """Build preference candidate paths in deterministic load order.

    Order:
    1. Root-level preference (`PREFERENCES.md` or fallback `PREFERENCES.default.md`)
    2. Channel prefixes from root to leaf:
       - `<prefix>.md`
       - `<prefix>/PREFERENCES.md`

    Args:
        in_channel: Input channel used for root-to-leaf prefix expansion.
        pref_root: Preference root directory, typically
            `<config_base>/preferences`.
    """

    out: list[Path] = _root_preference_candidates(pref_root)
    for prefix in iter_channel_prefixes(in_channel):
        out.append(pref_root / f"{prefix}.md")
        out.append(pref_root / prefix / "PREFERENCES.md")
    return out


def _contact_preference_candidates(
    *, contacts: list[str], pref_root: Path
) -> list[Path]:
    """Return user-level preference paths for `<platform>/<user_id>` contacts."""

    out: list[Path] = []
    seen: set[Path] = set()
    for contact in contacts:
        path = contact_preference_path(
            pref_root=pref_root,
            platform_contact=contact,
        )
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _load_preferences_prompt(
    *, in_channel: str, contacts: list[str], pref_root: Path
) -> str:
    """Load root-level, channel-prefix, and contact-level preferences.

    Preference files are resolved under `pref_root` (normally
    `<config_base>/preferences`).

    Returns:
        Empty string when no preference files match; otherwise a
        `<Preferences>...</Preferences>` block with one section per loaded file.
        Each section starts with its corresponding preference path. For
        symlinks, the path header also shows the resolved absolute target path.
    """

    candidates = _channel_preference_candidates(in_channel, pref_root=pref_root)
    candidates.extend(
        _contact_preference_candidates(contacts=contacts, pref_root=pref_root)
    )

    blocks: list[str] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        blocks.append(
            "\n".join([f"Path: {_display_preference_path(path)}", text, "---"])
        )

    if not blocks:
        return ""
    comment = "**The following are your preferences, written in your first person.**"
    return (
        f"<Preferences>\n{comment}\n" + "\n".join(blocks).rstrip() + "\n</Preferences>"
    )


def _display_preference_path(path: Path) -> str:
    """Return display text for preference path headers in system prompts.

    Normal files are shown as-is. Symlinks show both the symlink path and the
    resolved absolute target (`<path> -> <resolved_abs_target>`).
    """

    if not path.is_symlink():
        return str(path)
    return f"{path} -> {path.resolve()}"


def _validate_referenced_memory_ids(
    *,
    memory_store: MemoryStore,
    referenced_memory_ids: list[str],
) -> list[str]:
    """Validate referenced memory IDs emitted by `finish_action`.

    Contract:
        - Malformed ids still fail fast with `ModelRetry`.
        - Well-formed but missing ids are dropped. This keeps `finish_action`
          tolerant to stale references when records were deleted externally.
    """

    invalid_ids = [
        mem_id for mem_id in referenced_memory_ids if not is_memory_record_id(mem_id)
    ]
    if invalid_ids:
        raise ModelRetry(
            "Invalid referenced_memory_ids: each id must be a valid MemoryRecord id. "
            f"Invalid id(s): {invalid_ids}"
        )

    return [
        mem_id
        for mem_id in referenced_memory_ids
        if memory_store.get_by_id(mem_id) is not None
    ]


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
        `ctx.deps.working_memory_created_at` reserves the final record
        timestamp before model execution in `agent_run`; this tool must
        preserve that `created_at` so the `<System>` prompt and persisted
        memory record agree on the derived memory id.
    """

    validated_ids = _validate_referenced_memory_ids(
        memory_store=ctx.deps.memory_storage,
        referenced_memory_ids=referenced_memory_ids,
    )
    return MemoryRecord(
        created_at=ctx.deps.working_memory_created_at,
        in_channel=ctx.deps.start_event.in_channel,
        out_channel=ctx.deps.start_event.out_channel,
        contacts=ctx.deps.resolved_contact_ids,
        parents=validated_ids,
        input="",
        output=raw_output,
        compacted=[
            f"<input>{raw_input}</input>",
            f"<intents>{input_intents}</intents>",
            *compacted_actions,
            f"<output>{raw_output}</output>",
        ],
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
    """Inject channel-scoped preferences ahead of skill documents.

    Registration order matters: this function is intentionally declared before
    `concat_skills_prompt` so preference guidance appears first.
    """

    return _load_preferences_prompt(
        in_channel=ctx.deps.start_event.in_channel,
        contacts=ctx.deps.start_event.contacts,
        pref_root=ctx.deps.config.config_base / "preferences",
    )


@agent.system_prompt
def injected_memories_system_prompt(ctx: RunContext[MyDeps]) -> str:
    """Inject run-scoped memory context computed once in `agent_run`."""

    return ctx.deps.injected_memories_prompt


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


def _event_meta_prompt(event: Event) -> str:
    """Return a prompt chunk with event routing metadata (excluding body text).

    This keeps channel/routing context explicit for the model without duplicating
    the potentially large free-form `Event.content` body.
    """

    meta_json = event.model_dump_json(exclude={"content"})
    return f"<EventMeta>{meta_json}</EventMeta>\n"


async def _system_runtime_prompt(deps: MyDeps) -> str:
    """Return runtime metadata that should be explicit to the model.

    `Agent config base` is resolved through the shell runtime path (same
    transport as bash tools), not from Python process environment variables.
    The reserved run memory id is derived from a `created_at` timestamp
    captured before model execution so the prompt can name the final record
    explicitly.
    """

    try:
        runtime_config_base = await agent_config_base_value(
            basic_os_helper=deps.basic_os_helper,
            shell_manager=deps.shell_manager,
        )
    except Exception as exc:
        runtime_config_base = (
            f"<unresolved:{type(exc).__name__}:{str(exc).replace(chr(10), ' ')}>"
        )

    return (
        "<System>\n"
        f"Current time: {datetime.now()}\n"
        "Current run memory id: "
        f"{memory_record_id_from_created_at(deps.working_memory_created_at)}\n"
        "This id is reserved for the memory record produced by this run.\n"
        f"Agent config base (`{AGENT_CONFIG_BASE_EXPR}`): {runtime_config_base}\n"
        "</System>\n"
    )


def _memories_system_prompt(memory_blocks: Sequence[str]) -> str:
    """Serialize selected memory blocks into one `<Memories>` system prompt."""

    if not memory_blocks:
        return ""
    return f"<Memories>{'\n'.join(memory_blocks)}</Memories>"


def _select_auto_memory_records(
    memory_store: MemoryStore,
    *,
    in_channel: str,
    contacts: Sequence[str],
) -> tuple[list[str], set[str], list[str]]:
    """Merge channel/contact retrieval results for `agent_run`.

    Scope-specific retrieval lives in the sibling `k.agent.memory.retrieval.*`
    modules. This helper preserves the legacy cross-scope merge semantics:
    compacted ids win over raw-pair ids after the channel/contact results are
    combined, while `memory_parent_ids` remain the latest root ids per scope.
    Returned selected ids are sorted lexicographically, which matches
    `MemoryRecord.id_` time order.
    """

    channel_roots = latest_memory_roots_by_in_channel(
        memory_store,
        in_channel=in_channel,
    )
    contact_roots = latest_memory_roots_by_contact(
        memory_store,
        contacts=contacts,
    )
    channel_compacted_ids, channel_raw_pair_ids = select_memory_ids_by_in_channel(
        memory_store,
        in_channel=in_channel,
        roots=channel_roots,
    )
    contact_compacted_ids, contact_raw_pair_ids = select_memory_ids_by_contact(
        memory_store,
        contacts=contacts,
        roots=contact_roots,
    )

    compacted_ids = set(channel_compacted_ids) | set(contact_compacted_ids)
    selected_raw_pair_ids = (
        set(channel_raw_pair_ids) | set(contact_raw_pair_ids)
    ) - compacted_ids
    selected_ids = compacted_ids | selected_raw_pair_ids
    logger.info(
        "Injected memories counts (auto): "
        "channel_compacted_selected=%d, channel_raw_pair_selected=%d, "
        "contact_compacted_selected=%d, contact_raw_pair_selected=%d, "
        "injected_compacted=%d, injected_raw_pair=%d, injected_total=%d",
        len(channel_compacted_ids),
        len(channel_raw_pair_ids),
        len(contact_compacted_ids),
        len(contact_raw_pair_ids),
        len(compacted_ids),
        len(selected_raw_pair_ids),
        len(selected_ids),
    )
    return (
        sorted(selected_ids),
        compacted_ids,
        dedupe_memory_ids(channel_roots + contact_roots),
    )


async def agent_run(
    model: Model | KnownModelName,
    config: Config,
    memory_store: FolderMemoryStore,
    instruct: Event,
    message_history: Sequence[ModelMessage] | None = None,
    parent_memories: list[str] | None = None,
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

    Contact lifecycle:
    - Resolve `Event.contacts` platform ids to unique ids before model run.
    - Persist those resolved ids in the output `MemoryRecord.contacts`.

    Working-memory lifecycle:
    - Reserve the final `MemoryRecord.created_at` before entering `agent.run`.
    - Expose the derived memory id through `<System>`.
    - Preserve the same `created_at` in `finish_action`.

    Parent-memory selection:
    - `parent_memories` provided and non-empty: treat them as explicit root
      memory ids, resolve them through
      `k.agent.memory.utils.get_memory_ids_from_roots()`, and merge the
      resulting injections with the auto-generated scope injections below.
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

    resolved_contact_ids = resolve_contact_unique_ids(
        config_base=config.config_base,
        platform_contacts=instruct.contacts,
    )
    working_memory_created_at = datetime.now()
    explicit_parent_memory_ids = dedupe_memory_ids(parent_memories or [])
    selected_mem_ids, compacted_mem_ids, memory_parent_ids = (
        _select_auto_memory_records(
            memory_store,
            in_channel=instruct.in_channel,
            contacts=resolved_contact_ids,
        )
    )

    if explicit_parent_memory_ids:
        explicit_selected_ids, explicit_compacted_ids, explicit_root_ids = (
            get_memory_ids_from_roots(
                memory_store,
                roots=explicit_parent_memory_ids,
            )
        )
        selected_mem_ids = sorted(set(selected_mem_ids) | set(explicit_selected_ids))
        compacted_mem_ids |= explicit_compacted_ids
        memory_parent_ids = dedupe_memory_ids(explicit_root_ids + memory_parent_ids)
        logger.info(
            "Injected memories counts (explicit roots): "
            "roots=%d, injected_compacted=%d, injected_raw_pair=%d, "
            "injected_total=%d",
            len(explicit_root_ids),
            len(explicit_compacted_ids),
            len(explicit_selected_ids) - len(explicit_compacted_ids),
            len(explicit_selected_ids),
        )

    all_mem_rec = memory_store.get_by_ids(set(selected_mem_ids))
    memory_blocks = [
        x.dump_compated() if x.id_ in compacted_mem_ids else x.dump_raw_pair()
        for x in all_mem_rec
    ]
    memory_prompt = _memories_system_prompt(memory_blocks)

    usage_limits = UsageLimits()

    async with MyDeps(
        config=config,
        memory_storage=memory_store,
        memory_parents=memory_parent_ids,
        start_event=instruct,
        working_memory_created_at=working_memory_created_at,
        resolved_contact_ids=resolved_contact_ids,
        injected_memories_prompt=memory_prompt,
    ) as my_deps:
        res = await agent.run(
            model=model,
            deps=my_deps,
            user_prompt=(
                await _system_runtime_prompt(my_deps),
                _event_meta_prompt(instruct),
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
    memory_record = res.output
    memory_record.input = instruct.content
    memory_record.detailed = msgs

    return memory_record


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
