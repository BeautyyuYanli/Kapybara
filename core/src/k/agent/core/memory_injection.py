"""Memory selection and persistence pipeline for `k.agent.core.agent_run`.

Responsibilities:
- validate model-produced referenced memory ids before persistence
- select injected memory ids from auto scopes and explicit parent roots
- serialize the injected ids into the final `<Memories>` prompt block
- prepare the run-scoped memory state passed through `MyDeps`
- reattach explicit parent ids onto the final persisted `MemoryRecord`
- build the final `MemoryRecord` emitted by `finish_action`

Boundaries:
- `k.agent.core.agent` owns runtime prompt order, deps lifecycle, and
  `agent.run(...)` invocation.
- Channel/contact preference loading lives in
  `k.agent.core.preference_injection`.
- Scope-specific retrieval lives in `k.agent.memory.retrieval.*`.
- Generic root expansion and dedupe rules live in `k.agent.memory.utils`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from logging import getLogger
from pathlib import Path

from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ModelResponse

from k.agent.contacts import resolve_contact_unique_ids
from k.agent.core.entities import Event
from k.agent.memory.entities import MemoryRecord, is_memory_record_id
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

logger = getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemorySelection:
    """Selected ids plus rendering metadata for one injection source."""

    selected_ids: list[str]
    compacted_ids: frozenset[str]
    memory_parent_ids: list[str]


@dataclass(frozen=True, slots=True)
class MemoryInjectionContext:
    """Prompt-ready memory context for one `agent_run` invocation."""

    injected_memories_prompt: str
    memory_parents: list[str]
    explicit_parent_memory_ids: list[str]


@dataclass(slots=True)
class MemoryRunState:
    """Prepared memory state shared by `agent_run`, prompts, and `finish_action`.

    `working_memory_created_at` reserves the final output record id before the
    model runs. The remaining fields carry the injected `<Memories>` prompt,
    deduped inherited parent ids, and the resolved contact ids that must be
    persisted into the final `MemoryRecord`.
    """

    working_memory_created_at: datetime
    resolved_contact_ids: list[str]
    injected_memories_prompt: str
    memory_parents: list[str]
    explicit_parent_memory_ids: list[str]


def validate_referenced_memory_ids(
    *,
    memory_store: MemoryStore,
    referenced_memory_ids: list[str],
) -> list[str]:
    """Validate referenced memory ids emitted by `finish_action`.

    Contract:
    - malformed ids still fail fast with `ModelRetry`
    - well-formed but missing ids are dropped to tolerate externally deleted
      records
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


def append_explicit_parent_memory_ids(
    *,
    memory_store: MemoryStore,
    referenced_memory_ids: list[str],
    explicit_parent_memory_ids: Sequence[str],
) -> list[str]:
    """Append validated explicit parents onto already-validated run parents."""

    validated_explicit_parent_ids = validate_referenced_memory_ids(
        memory_store=memory_store,
        referenced_memory_ids=dedupe_memory_ids(explicit_parent_memory_ids),
    )
    return dedupe_memory_ids(referenced_memory_ids + validated_explicit_parent_ids)


def select_auto_memory_records(
    memory_store: MemoryStore,
    *,
    in_channel: str,
    contacts: Sequence[str],
) -> MemorySelection:
    """Merge channel/contact auto-retrieval results for one `agent_run`."""

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
    return MemorySelection(
        selected_ids=sorted(selected_ids),
        compacted_ids=frozenset(compacted_ids),
        memory_parent_ids=dedupe_memory_ids(channel_roots + contact_roots),
    )


def select_explicit_parent_memory_records(
    memory_store: MemoryStore,
    *,
    parent_memory_ids: Sequence[str],
    compacted_cap_num: int = 15,
    raw_pair_level_num: int = 3,
    raw_pair_cap_num: int = 15,
) -> MemorySelection:
    """Resolve explicit `parent_memories` without truncating the roots."""

    if compacted_cap_num < 0:
        raise ValueError(
            f"explicit_parent_compacted_cap_num must be >= 0; got {compacted_cap_num}"
        )
    if raw_pair_cap_num < 0:
        raise ValueError(
            f"explicit_parent_raw_pair_cap_num must be >= 0; got {raw_pair_cap_num}"
        )

    deduped_root_ids = dedupe_memory_ids(parent_memory_ids)
    if not deduped_root_ids:
        return MemorySelection(
            selected_ids=[],
            compacted_ids=frozenset(),
            memory_parent_ids=[],
        )

    expand_ancestors = len(deduped_root_ids) < compacted_cap_num
    selected_ids, compacted_ids, root_ids = get_memory_ids_from_roots(
        memory_store,
        roots=deduped_root_ids,
        compacted_level_num=0,
        raw_pair_level_num=raw_pair_level_num if expand_ancestors else 0,
        compacted_cap_num=max(compacted_cap_num, len(deduped_root_ids)),
        raw_pair_cap_num=raw_pair_cap_num if expand_ancestors else 0,
        cap_name_prefix="explicit_parent",
    )
    logger.info(
        "Injected memories counts (explicit roots): "
        "roots=%d, injected_compacted=%d, injected_raw_pair=%d, "
        "injected_total=%d",
        len(root_ids),
        len(compacted_ids),
        len(selected_ids) - len(compacted_ids),
        len(selected_ids),
    )
    return MemorySelection(
        selected_ids=selected_ids,
        compacted_ids=frozenset(compacted_ids),
        memory_parent_ids=root_ids,
    )


def build_memory_injection_context(
    memory_store: MemoryStore,
    *,
    in_channel: str,
    contacts: Sequence[str],
    parent_memories: Sequence[str] | None = None,
) -> MemoryInjectionContext:
    """Build the prompt + parent ids injected into one `agent_run`."""

    explicit_parent_memory_ids = dedupe_memory_ids(parent_memories or [])
    auto_selection = select_auto_memory_records(
        memory_store,
        in_channel=in_channel,
        contacts=contacts,
    )

    selected_ids = set(auto_selection.selected_ids)
    compacted_ids = set(auto_selection.compacted_ids)
    memory_parent_ids = list(auto_selection.memory_parent_ids)

    if explicit_parent_memory_ids:
        explicit_selection = select_explicit_parent_memory_records(
            memory_store,
            parent_memory_ids=explicit_parent_memory_ids,
        )
        selected_ids.update(explicit_selection.selected_ids)
        compacted_ids.update(explicit_selection.compacted_ids)
        memory_parent_ids = dedupe_memory_ids(
            explicit_selection.memory_parent_ids + memory_parent_ids
        )

    all_memories = memory_store.get_by_ids(set(selected_ids))
    memory_blocks = [
        record.dump_compated()
        if record.id_ in compacted_ids
        else record.dump_raw_pair()
        for record in all_memories
    ]
    return MemoryInjectionContext(
        injected_memories_prompt=_memories_system_prompt(memory_blocks),
        memory_parents=memory_parent_ids,
        explicit_parent_memory_ids=explicit_parent_memory_ids,
    )


def prepare_memory_run_state(
    memory_store: MemoryStore,
    *,
    config_base: Path,
    instruct: Event,
    parent_memories: Sequence[str] | None = None,
    working_memory_created_at: datetime | None = None,
) -> MemoryRunState:
    """Prepare the full memory state needed for one `agent_run` invocation."""

    resolved_contact_ids = resolve_contact_unique_ids(
        config_base=config_base,
        platform_contacts=instruct.contacts,
    )
    injection_context = build_memory_injection_context(
        memory_store,
        in_channel=instruct.in_channel,
        contacts=resolved_contact_ids,
        parent_memories=parent_memories,
    )
    return MemoryRunState(
        working_memory_created_at=working_memory_created_at or datetime.now(),
        resolved_contact_ids=resolved_contact_ids,
        injected_memories_prompt=injection_context.injected_memories_prompt,
        memory_parents=injection_context.memory_parents,
        explicit_parent_memory_ids=injection_context.explicit_parent_memory_ids,
    )


def build_finish_action_record(
    *,
    memory_store: MemoryStore,
    memory_run: MemoryRunState,
    start_event: Event,
    referenced_memory_ids: list[str],
    raw_input: str,
    raw_output: str,
    input_intents: str,
    compacted_actions: list[str],
) -> MemoryRecord:
    """Build the `MemoryRecord` emitted by `finish_action`.

    The referenced ids are validated against the current store contents, while
    `working_memory_created_at` and resolved contacts come from the prepared
    run state captured before model execution.
    """

    validated_ids = validate_referenced_memory_ids(
        memory_store=memory_store,
        referenced_memory_ids=referenced_memory_ids,
    )
    return MemoryRecord(
        created_at=memory_run.working_memory_created_at,
        in_channel=start_event.in_channel,
        out_channel=start_event.out_channel,
        contacts=memory_run.resolved_contact_ids,
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


def finalize_memory_record(
    *,
    memory_store: MemoryStore,
    memory_run: MemoryRunState,
    memory_record: MemoryRecord,
    instruct_content: str,
    detailed_messages: list[ModelRequest | ModelResponse],
) -> MemoryRecord:
    """Apply post-run memory finalization before returning the output record."""

    memory_record.parents = append_explicit_parent_memory_ids(
        memory_store=memory_store,
        referenced_memory_ids=memory_record.parents,
        explicit_parent_memory_ids=memory_run.explicit_parent_memory_ids,
    )
    memory_record.input = instruct_content
    memory_record.detailed = detailed_messages
    return memory_record


def _memories_system_prompt(memory_blocks: Sequence[str]) -> str:
    """Serialize selected memory blocks into one `<Memories>` system prompt."""

    if not memory_blocks:
        return ""
    return f"<Memories>{'\n'.join(memory_blocks)}</Memories>"
