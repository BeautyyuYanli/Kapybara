"""Compatibility helpers and CLI entrypoint.

`agent_run` and `MyDeps` live in `k.agent.core.agent` (per architecture).
This module keeps small helpers that are useful for callers/tests and the CLI
loop entrypoint.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.messages import UserContent

from k.agent.channels import channel_root
from k.agent.core.agent import agent_run
from k.agent.core.entities import Event
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config


def _extract_input_event_channel_root(instruct: Sequence[UserContent]) -> str | None:
    """Best-effort extraction of an input channel root from Event JSON.

    `agent_run` typically receives a structured `Event` JSON as the first user
    prompt item (e.g. from Telegram polling). When present, we use it to inject
    channel-root specific skills in system prompts.
    """

    for item in instruct:
        if not isinstance(item, str):
            continue
        try:
            event = Event.model_validate_json(item)
        except Exception:
            continue
        return channel_root(event.in_channel)
    return None


def claim_read_and_empty(path: str) -> str:
    import os
    import uuid

    claimed = f"{path}.{uuid.uuid4().hex}.claimed"

    # Atomic on POSIX when source+target are on same filesystem
    os.replace(path, claimed)

    # Recreate empty file at original path
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.close(fd)

    # Now read the claimed old contents
    with open(claimed, encoding="utf-8") as f:
        data = f.read()

    os.remove(claimed)

    return data


async def main() -> None:
    from pydantic_ai.models.openrouter import OpenRouterModel
    from rich import print

    config = Config()  # type: ignore
    model = OpenRouterModel("openai/gpt-5.2")
    mem_store = FolderMemoryStore(
        root=config.fs_base / "memories",
    )
    while True:
        i = input("\n> ")
        if i.lower() in {"exit", "quit"}:
            print("Exiting the agent loop.")
            break
        output, mem = await agent_run(
            model,
            config,
            mem_store,
            Event(in_channel="direct_input", content=i),
        )
        print(output)
        mem_store.append(mem)
        print(mem.compacted)
