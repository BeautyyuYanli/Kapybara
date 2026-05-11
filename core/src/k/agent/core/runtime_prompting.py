"""Runtime prompt builders shared by `k.agent.core.agent`.

These helpers keep `agent.py` focused on agent wiring while preserving the
current prompt ordering and runtime metadata contract.
"""

from __future__ import annotations

from datetime import datetime

from shell_session_manager import ShellSessionManager

from k.agent.core.entities import Event
from k.agent.memory.entities import memory_record_id_from_created_at
from k.runner_helpers.basic_os import (
    AGENT_CONFIG_BASE_EXPR,
    BasicOSHelper,
    agent_config_base_value,
)


def event_meta_prompt(event: Event) -> str:
    """Return a prompt chunk with event routing metadata (excluding body text).

    This keeps channel/routing context explicit for the model without duplicating
    the potentially large free-form `Event.content` body.
    """

    meta_json = event.model_dump_json(exclude={"content"})
    return f"<EventMeta>{meta_json}</EventMeta>\n"


async def system_runtime_prompt(
    *,
    basic_os_helper: BasicOSHelper,
    shell_manager: ShellSessionManager,
    working_memory_created_at: datetime,
) -> str:
    """Return runtime metadata that should be explicit to the model.

    `Agent config base` is resolved through the shell runtime path (same
    transport as bash tools), not from Python process environment variables.
    The reserved run memory id is derived from a `created_at` timestamp
    captured before model execution so the prompt can name the final record
    explicitly.
    """

    try:
        runtime_config_base = await agent_config_base_value(
            basic_os_helper=basic_os_helper,
            shell_manager=shell_manager,
        )
    except Exception as exc:
        runtime_config_base = (
            f"<unresolved:{type(exc).__name__}:{str(exc).replace(chr(10), ' ')}>"
        )

    return (
        "<System>\n"
        f"Current time: {datetime.now()}\n"
        "Current run memory id: "
        f"{memory_record_id_from_created_at(working_memory_created_at)}\n"
        "This id is reserved for the memory record produced by this run.\n"
        f"Agent config base (`{AGENT_CONFIG_BASE_EXPR}`): {runtime_config_base}\n"
        "</System>\n"
    )
