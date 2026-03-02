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
    The loaded preference content is injected as a dynamic system prompt before
    skills are concatenated.

Memory compaction contract:
    The canonical high-fidelity `compacted_actions` prompt lives in
    `k.agent.core.prompts.compacted_prompt`
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
from k.agent.memory.entities import MemoryRecord, is_memory_record_id
from k.agent.memory.folder import FolderMemoryStore
from k.agent.memory.paths import memory_root_from_config_base
from k.agent.memory.store import MemoryStore
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
    resolved_contact_ids: list[str]
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
        `ctx.deps.resolved_contact_ids` is prepared before model execution in
        `agent_run` and persisted directly to `MemoryRecord.contacts`.
    """

    validated_ids = _validate_referenced_memory_ids(
        memory_store=ctx.deps.memory_storage,
        referenced_memory_ids=referenced_memory_ids,
    )
    return MemoryRecord(
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

    `AgentConfigBase` is resolved through the shell runtime path (same transport
    as bash tools), not from Python process environment variables.
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
        f"Now: {datetime.now()}\n"
        f"Value of `{AGENT_CONFIG_BASE_EXPR}`: {runtime_config_base}\n"
        "</System>\n"
    )


def _dedupe_memory_ids(ids: Sequence[str]) -> list[str]:
    """Return ids in first-seen order with duplicates removed."""

    out: list[str] = []
    seen: set[str] = set()
    for id_ in ids:
        if id_ in seen:
            continue
        seen.add(id_)
        out.append(id_)
    return out


def _resolve_auto_parent_memory_ids(
    *,
    memory_store: FolderMemoryStore,
    in_channel: str,
    contacts: list[str],
    in_channel_latest_num: int = 5,
    contact_latest_num: int = 1,
) -> tuple[list[str], list[str]]:
    """Resolve auto-selected memory roots for `agent_run`.

    This helper is used only when `parent_memories` is omitted/empty.

    Returns:
        A tuple `(channel_roots, contact_roots)` where:
        - `channel_roots`: latest records by `in_channel` subtree.
        - `contact_roots`: latest records per contact id.

    Deduplication is stable and first-seen.
    """

    channel_roots: list[str] = []
    if in_channel_latest_num > 0:
        channel_roots.extend(
            memory_store.get_latests(
                in_channel=in_channel,
                num=in_channel_latest_num,
            )
        )

    contact_roots: list[str] = []
    if contact_latest_num > 0:
        for contact in contacts:
            contact_roots.extend(
                memory_store.get_latests(
                    contact=contact,
                    num=contact_latest_num,
                )
            )

    return _dedupe_memory_ids(channel_roots), _dedupe_memory_ids(contact_roots)


def _keep_latest_records(
    records: list[MemoryRecord],
    *,
    cap_num: int,
    name: str,
) -> list[MemoryRecord]:
    """Return latest `cap_num` records from a datetime-sorted list."""

    if cap_num < 0:
        raise ValueError(f"{name} must be >= 0; got {cap_num}")
    if cap_num == 0:
        return []
    return records[-cap_num:]


async def _memory_select(
    memory_store: FolderMemoryStore,
    *,
    channel_parent_memories: list[str],
    contact_parent_memories: list[str],
    channel_compacted_level_num: int = 1,
    channel_raw_pair_level_num: int = 3,
    channel_compacted_cap_num: int = 15,
    channel_raw_pair_cap_num: int = 15,
    contact_compacted_level_num: int = 0,
    contact_raw_pair_level_num: int = 1,
    contact_compacted_cap_num: int = 5,
    contact_raw_pair_cap_num: int = 5,
):
    """Select memory records for compacted/raw-pair injection.

    This performs independent selection for two scopes and then unions them:
    - channel scope:
      roots=`channel_parent_memories`,
      compacted ancestors=`channel_compacted_level_num`,
      raw-pair ancestors=`channel_raw_pair_level_num`,
      caps=`channel_compacted_cap_num` / `channel_raw_pair_cap_num`.
    - contact scope:
      roots=`contact_parent_memories`,
      compacted ancestors=`contact_compacted_level_num`,
      raw-pair ancestors=`contact_raw_pair_level_num`,
      caps=`contact_compacted_cap_num` / `contact_raw_pair_cap_num`.

    Within each scope, compacted overflow is downgraded into raw-pair
    candidates before applying raw-pair caps.

    Side effects:
    - Emits `logger.info` with selected per-category counts and final
      injected counts after global deduplication.

    Returned records are sorted by datetime (same order as `get_by_ids`).
    """

    def _select_scope(
        *,
        scope_name: str,
        roots: list[str],
        compacted_level_num: int,
        raw_pair_level_num: int,
        compacted_cap_num: int,
        raw_pair_cap_num: int,
    ) -> tuple[set[str], set[str]]:
        compacted_mem = set(roots)
        raw_mem = set(roots)
        for mem in roots:
            compacted_mem.update(
                memory_store.get_ancestors(mem, level=compacted_level_num)
            )
            raw_mem.update(memory_store.get_ancestors(mem, level=raw_pair_level_num))

        raw_pair_only = raw_mem - compacted_mem
        compacted_source_records = memory_store.get_by_ids(compacted_mem)
        compacted_records = _keep_latest_records(
            compacted_source_records,
            cap_num=compacted_cap_num,
            name=f"{scope_name}_compacted_cap_num",
        )
        compacted_ids = {rec.id_ for rec in compacted_records}
        downgraded_ids = {rec.id_ for rec in compacted_source_records} - compacted_ids
        raw_pair_candidates = raw_pair_only | downgraded_ids
        raw_pair_records = _keep_latest_records(
            memory_store.get_by_ids(raw_pair_candidates),
            cap_num=raw_pair_cap_num,
            name=f"{scope_name}_raw_pair_cap_num",
        )
        raw_pair_ids = {rec.id_ for rec in raw_pair_records}
        return compacted_ids, raw_pair_ids

    channel_compacted_ids, channel_raw_pair_ids = _select_scope(
        scope_name="channel",
        roots=channel_parent_memories,
        compacted_level_num=channel_compacted_level_num,
        raw_pair_level_num=channel_raw_pair_level_num,
        compacted_cap_num=channel_compacted_cap_num,
        raw_pair_cap_num=channel_raw_pair_cap_num,
    )
    contact_compacted_ids, contact_raw_pair_ids = _select_scope(
        scope_name="contact",
        roots=contact_parent_memories,
        compacted_level_num=contact_compacted_level_num,
        raw_pair_level_num=contact_raw_pair_level_num,
        compacted_cap_num=contact_compacted_cap_num,
        raw_pair_cap_num=contact_raw_pair_cap_num,
    )

    capped_recent_ids = channel_compacted_ids | contact_compacted_ids
    channel_raw_pair_selected = channel_raw_pair_ids
    contact_raw_pair_selected = contact_raw_pair_ids
    channel_raw_pair_injected = channel_raw_pair_selected - capped_recent_ids
    contact_raw_pair_injected = contact_raw_pair_selected - capped_recent_ids
    selected_raw_pair_ids = channel_raw_pair_injected | contact_raw_pair_injected
    selected_ids = capped_recent_ids | selected_raw_pair_ids
    logger.info(
        "Injected memories counts (auto): "
        "channel_compacted_selected=%d, channel_raw_pair_selected=%d, "
        "contact_compacted_selected=%d, contact_raw_pair_selected=%d, "
        "injected_compacted=%d, injected_raw_pair=%d, injected_total=%d",
        len(channel_compacted_ids),
        len(channel_raw_pair_selected),
        len(contact_compacted_ids),
        len(contact_raw_pair_selected),
        len(capped_recent_ids),
        len(selected_raw_pair_ids),
        len(selected_ids),
    )
    all_mem_rec = memory_store.get_by_ids(selected_ids)
    return all_mem_rec, capped_recent_ids


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
    1. optional `<Memories>...</Memories>` context
    2. `<System>Now + agent config-base runtime view</System>`
    3. `<EventMeta>...</EventMeta>`
    4. real instruction content (`Event.content`)

    Contact lifecycle:
    - Resolve `Event.contacts` platform ids to unique ids before model run.
    - Persist those resolved ids in the output `MemoryRecord.contacts`.

    Parent-memory selection:
    - `parent_memories` provided and non-empty: inject directly into
      `<Memories>` without MemoryStore lookup/parsing.
    - `parent_memories` omitted or empty: auto-generate from MemoryStore:
      1) latest 5 records in `in_channel`, expand with 1-level compacted
         ancestors + 3-level raw-pair-only ancestors.
      2) latest 1 record per resolved contact id, expand with 1-level
         raw-pair-only ancestors.
      Caps:
      - channel scope: compacted 15, raw-pair 15
      - contact scope: compacted 5, raw-pair 5
    """

    resolved_contact_ids = resolve_contact_unique_ids(
        config_base=config.config_base,
        platform_contacts=instruct.contacts,
    )
    explicit_memories = list(parent_memories or [])
    memory_parent_ids: list[str] = []
    memory_blocks: list[str] = []

    if explicit_memories:
        memory_blocks = explicit_memories
        logger.info(
            "Injected memories counts (explicit): explicit=%d, injected_total=%d",
            len(memory_blocks),
            len(memory_blocks),
        )
    else:
        channel_roots, contact_roots = _resolve_auto_parent_memory_ids(
            memory_store=memory_store,
            in_channel=instruct.in_channel,
            contacts=resolved_contact_ids,
        )
        all_mem_rec, recent_mem = await _memory_select(
            memory_store,
            channel_parent_memories=channel_roots,
            contact_parent_memories=contact_roots,
        )
        memory_parent_ids = _dedupe_memory_ids(channel_roots + contact_roots)
        memory_blocks = [
            x.dump_compated() if x.id_ in recent_mem else x.dump_raw_pair()
            for x in all_mem_rec
        ]

    memory_string = "\n".join(memory_blocks)

    async with MyDeps(
        config=config,
        memory_storage=memory_store,
        memory_parents=memory_parent_ids,
        start_event=instruct,
        resolved_contact_ids=resolved_contact_ids,
    ) as my_deps:
        res = await agent.run(
            model=model,
            deps=my_deps,
            user_prompt=(
                f"<Memories>{memory_string}</Memories>\n" if memory_blocks else "",
                await _system_runtime_prompt(my_deps),
                _event_meta_prompt(instruct),
                instruct.content,
            ),
            message_history=message_history,
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
