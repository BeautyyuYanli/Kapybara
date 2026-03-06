"""Compatibility helpers and installed CLI wrapper.

`agent_run` and `MyDeps` live in `k.agent.core.agent` (per architecture).
This module keeps small helpers that are useful for callers/tests plus the
direct-input `kapy` console script.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

import anyio
import logfire
from pydantic_ai.messages import UserContent

from k.agent.channels import channel_root
from k.agent.core.agent import agent_run
from k.agent.core.entities import Event
from k.agent.memory.folder import FolderMemoryStore
from k.agent.memory.paths import memory_root_from_config_base
from k.agent.memory.store import MemoryStore, coerce_record_id
from k.config import Config, load_kapybara_toml_config

if TYPE_CHECKING:
    from pydantic_ai.models import Model


logger = logging.getLogger(__name__)


def _configure_cli_logfire() -> None:
    """Configure Logfire for CLI runs without requiring project credentials.

    The default `logfire.configure()` behavior prompts for project setup when no
    token or cached credentials are present. `kapy` is often used in ad-hoc or
    automated shells, so only send telemetry when Logfire is already configured.
    """

    logfire.configure(send_to_logfire="if-token-present")
    logfire.instrument_pydantic_ai()
    logging.basicConfig(level=logging.INFO, handlers=[logfire.LogfireLoggingHandler()])


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
    """Atomically swap `path` aside, recreate it empty, then return old text."""

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


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse installed `kapy` CLI arguments.

    `config_base` is no longer configurable on the command line; the process
    always uses `Config()` so the default/env-backed config root stays
    consistent with the rest of the runtime. When `--in-channel` is omitted,
    the default is resolved later from the execution mode: one-shot runs get a
    fresh `direct/<random>` channel, while the REPL uses `direct/default`.
    """

    parser = argparse.ArgumentParser(
        prog="kapy",
        description="Direct-input CLI wrapper around k.agent.core.agent_run.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Optional one-shot prompt. When omitted, start an interactive REPL.",
    )
    parser.add_argument(
        "--in-channel",
        default=None,
        help=(
            "Event.in_channel used for prompts sent through this CLI. "
            "Defaults to direct/<random> for one-shot prompts and "
            "direct/default for the REPL."
        ),
    )
    parser.add_argument(
        "--contact",
        action="append",
        dest="contacts",
        default=None,
        help="Optional Event contact in <platform>/<user_id> form. Repeatable.",
    )
    parser.add_argument(
        "--out-channel",
        default=None,
        help="Optional Event.out_channel override.",
    )
    parser.add_argument(
        "--parent-memory",
        action="append",
        dest="parent_memories",
        default=None,
        help="Optional parent memory id to inject. Repeatable.",
    )
    parser.add_argument(
        "--parents-timeout-seconds",
        type=float,
        default=300.0,
        help=(
            "How long to wait for all --parent-memory ids to exist before "
            "cancelling. Default: 300 seconds."
        ),
    )
    return parser.parse_args(argv)


def _agent_run_model_from_config(config: Config) -> Model:
    """Build the `agent_run` model from `<config_base>/config.toml`.

    `kapy` uses `OpenAIChatModel`, so `model_name` must be an OpenAI model id
    such as `gpt-5.2`.
    """

    from pydantic_ai.models.openai import OpenAIChatModel

    file_config = load_kapybara_toml_config(config.config_base)
    return OpenAIChatModel(file_config.agent_run.model_name)


def _normalize_parent_memory_ids(parent_memories: list[str] | None) -> list[str]:
    """Validate parent memory ids, dropping duplicates while preserving order."""

    out: list[str] = []
    seen: set[str] = set()
    for parent_id in parent_memories or []:
        normalized = coerce_record_id(parent_id)
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _resolve_cli_in_channel(in_channel: str | None, *, repl: bool) -> str:
    """Return a CLI `in_channel`, filling mode-specific defaults when omitted.

    One-shot `kapy` runs should not share history by default, so omitted
    `--in-channel` generates a fresh `direct/<random>` path per invocation.
    The REPL keeps the stable `direct/default` channel so repeated turns share
    context until the session exits.
    """

    if in_channel is not None:
        return in_channel
    if repl:
        return "direct/default"
    return f"direct/{uuid.uuid4().hex}"


def _resolve_cli_contacts(contacts: list[str] | None) -> list[str]:
    """Resolve CLI contacts without injecting synthetic defaults.

    `kapy` should preserve the caller-supplied contact set exactly. Omitting
    `--contact` now means "run without contacts" instead of manufacturing a
    placeholder contact.
    """

    return list(contacts or [])


async def _wait_for_parent_memories(
    *,
    memory_store: MemoryStore,
    parent_memories: list[str],
    timeout_seconds: float,
    poll_interval_seconds: float = 1.0,
) -> None:
    """Wait until every parent memory id exists in `memory_store`.

    This is intended for cross-process orchestration where one `kapy` command
    depends on parent records written by another process shortly beforehand.
    The default poll interval is 1 second to keep detached follow-up waiting
    cheap while still reacting promptly once the parent record lands.

    Unexpected storage errors are logged with the full parent-id set before
    being re-raised so detached CLI waits do not appear to stall silently.

    Raises:
        ValueError: if `timeout_seconds` is not positive.
        TimeoutError: if any parent memory ids are still missing at timeout.
    """

    if timeout_seconds <= 0:
        raise ValueError(f"parents_timeout_seconds must be > 0; got {timeout_seconds}")
    if not parent_memories:
        return

    deadline = time.monotonic() + timeout_seconds
    parent_ids_str = ", ".join(parent_memories)
    try:
        while True:
            # Parent memories may be produced by another process. `refresh()` is a
            # compatibility hook here; the subsequent `get_by_id()` must observe the
            # latest on-disk state even when this store keeps no derived indexes.
            memory_store.refresh()
            missing = [
                parent_id
                for parent_id in parent_memories
                if memory_store.get_by_id(parent_id) is None
            ]
            if not missing:
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                missing_str = ", ".join(missing)
                raise TimeoutError(
                    "Timed out waiting for parent memories: " + missing_str
                )
            await anyio.sleep(min(poll_interval_seconds, remaining))
    except TimeoutError:
        logger.error("Timed out waiting for parent memories: %s", parent_ids_str)
        raise
    except Exception:
        logger.exception("Failed while waiting for parent memories: %s", parent_ids_str)
        raise


async def run_once(
    *,
    config: Config,
    memory_store: FolderMemoryStore,
    prompt: str,
    in_channel: str | None = None,
    contacts: list[str] | None = None,
    out_channel: str | None = None,
    parent_memories: list[str] | None = None,
    parents_timeout_seconds: float = 300.0,
    model: Model | None = None,
) -> str:
    """Run one prompt through `agent_run` and append the resulting memory.

    Side effects:
    - Reads `<config_base>/config.toml` when `model` is omitted.
    - Waits for caller-specified `parent_memories` to exist before running.
    - Appends the returned `MemoryRecord` to `memory_store`.

    Contact semantics:
    - `contacts=None` and `contacts=[]` both preserve an empty
      `Event.contacts` list.
    - `in_channel=None` generates a fresh `direct/<random>` channel so
      one-shot CLI invocations do not accidentally reuse unrelated history.
    """

    resolved_model = model or _agent_run_model_from_config(config)
    resolved_in_channel = _resolve_cli_in_channel(in_channel, repl=False)
    resolved_parent_memories = _normalize_parent_memory_ids(parent_memories)
    resolved_contacts = _resolve_cli_contacts(contacts)
    if resolved_parent_memories:
        await _wait_for_parent_memories(
            memory_store=memory_store,
            parent_memories=resolved_parent_memories,
            timeout_seconds=parents_timeout_seconds,
        )
    event = Event(
        in_channel=resolved_in_channel,
        contacts=resolved_contacts,
        out_channel=out_channel,
        content=prompt,
    )
    mem = await agent_run(
        model=resolved_model,
        config=config,
        memory_store=memory_store,
        instruct=event,
        parent_memories=resolved_parent_memories,
    )
    memory_store.append(mem)
    return mem.dump_compated()


async def run_repl(
    *,
    config: Config,
    memory_store: FolderMemoryStore,
    in_channel: str | None = None,
    contacts: list[str] | None = None,
    out_channel: str | None = None,
    parent_memories: list[str] | None = None,
    parents_timeout_seconds: float = 300.0,
    model: Model | None = None,
) -> None:
    """Run the installed direct-input REPL until the user exits.

    `in_channel=None` resolves to `direct/default`, giving the REPL one stable
    conversation channel for the whole interactive session.
    """

    from rich import print

    resolved_model = model or _agent_run_model_from_config(config)
    # Keep a stable REPL channel for the whole session so turns share memory.
    resolved_in_channel = _resolve_cli_in_channel(in_channel, repl=True)
    while True:
        i = input("\n> ")
        if i.lower() in {"exit", "quit"}:
            print("Exiting the agent loop.")
            break
        print(
            await run_once(
                config=config,
                memory_store=memory_store,
                prompt=i,
                in_channel=resolved_in_channel,
                contacts=_resolve_cli_contacts(contacts),
                out_channel=out_channel,
                parent_memories=parent_memories,
                parents_timeout_seconds=parents_timeout_seconds,
                model=resolved_model,
            )
        )


async def main(argv: list[str] | None = None) -> None:
    """Async CLI entrypoint for the installed `kapy` console script."""

    from rich import print

    _configure_cli_logfire()
    args = _parse_cli_args(argv)
    config = Config()
    memory_store = FolderMemoryStore(
        root=memory_root_from_config_base(config.config_base),
    )
    model = _agent_run_model_from_config(config)
    contacts = _resolve_cli_contacts(args.contacts)
    parent_memories = _normalize_parent_memory_ids(args.parent_memories)

    if args.prompt is not None:
        print(
            await run_once(
                config=config,
                memory_store=memory_store,
                prompt=args.prompt,
                in_channel=args.in_channel,
                contacts=contacts,
                out_channel=args.out_channel,
                parent_memories=parent_memories,
                parents_timeout_seconds=args.parents_timeout_seconds,
                model=model,
            )
        )
        return

    await run_repl(
        config=config,
        memory_store=memory_store,
        in_channel=args.in_channel,
        contacts=contacts,
        out_channel=args.out_channel,
        parent_memories=parent_memories,
        parents_timeout_seconds=args.parents_timeout_seconds,
        model=model,
    )


def cli() -> None:
    """Synchronous console-script shim for `kapy`."""

    asyncio.run(main())


if __name__ == "__main__":
    cli()
