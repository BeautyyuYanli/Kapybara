"""Compatibility helpers and installed one-shot CLI wrapper.

`agent_run` and `MyDeps` live in `k.agent.core.agent` (per architecture).
This module keeps small helpers that are useful for callers/tests plus the
installed `kapy` console script.

The CLI is one-shot only: callers must pass a prompt, foreground runs use
`--wait`, and detached execution is the default otherwise. Stdout is reserved
for exactly one JSON value per invocation. Foreground runs emit the final
memory JSON, while detached launches emit child metadata JSON and redirect all
child output into a fresh logfile under `<config_base>/logs/kapy/`.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import logfire
from pydantic import BaseModel, ConfigDict
from pydantic_ai.messages import UserContent

from k.agent.channels import channel_root
from k.agent.core.agent import agent_run
from k.agent.core.entities import Event
from k.agent.memory.entities import memory_record_id_from_created_at
from k.agent.memory.folder import FolderMemoryStore
from k.agent.memory.paths import memory_root_from_config_base
from k.agent.memory.store import MemoryStore, coerce_record_id
from k.config import Config, load_kapybara_toml_config

if TYPE_CHECKING:
    from pydantic_ai.models import Model


logger = logging.getLogger(__name__)
agent_module = importlib.import_module("k.agent.core.agent")


class DetachedCliRunMetadata(BaseModel):
    """Machine-facing stdout payload for one detached CLI launch.

    `memory_id` is reserved before the child starts so orchestrators can wire
    dependent runs immediately. `logfile` stays typed as `Path` in Python and
    is serialized to a JSON string on stdout.
    """

    model_config = ConfigDict(extra="forbid")

    pid: int
    memory_id: str
    logfile: Path


def _load_cli_logfire_token(config_base: str | Path) -> str | None:
    """Best-effort lookup of `[logfire].token` from `<config_base>/config.toml`.

    Logfire setup should not fail earlier than the main CLI config path. When
    the TOML file is missing or invalid, keep the previous environment-driven
    behavior here and let the later model-config load surface the real error.
    """

    try:
        file_config = load_kapybara_toml_config(config_base)
    except ValueError:
        return None

    if file_config.logfire is None:
        return None
    return file_config.logfire.token


def _configure_cli_logfire(config_base: str | Path) -> None:
    """Configure Logfire for CLI runs without requiring project credentials.

    The default `logfire.configure()` behavior prompts for project setup when no
    token or cached credentials are present. `kapy` is often used in ad-hoc or
    automated shells, so only send telemetry when Logfire is already configured.
    If `<config_base>/config.toml` declares `[logfire].token`, prefer that
    token for `kapy` CLI runs.
    """

    token = _load_cli_logfire_token(config_base)
    if token is None:
        logfire.configure(send_to_logfire="if-token-present")
    else:
        logfire.configure(send_to_logfire="if-token-present", token=token)
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


def _normalize_parent_memory_flag_values(
    argv: Sequence[str] | None = None,
) -> list[str]:
    """Attach dashed parent-memory ids to `--parent-memory=` before parsing.

    Memory record ids use an ordered base64 alphabet where `-` is a valid first
    character, so callers often pass values such as `--parent-memory --------`.
    `argparse` treats that second token as another option rather than as the
    value for `--parent-memory`. Rewriting only syntactically valid record ids
    into the `--parent-memory=<id>` form keeps normal option parsing intact
    while preserving the usual missing/invalid-value errors for everything else.
    """

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    normalized: list[str] = []
    idx = 0
    while idx < len(raw_argv):
        token = raw_argv[idx]
        if token != "--parent-memory" or idx + 1 >= len(raw_argv):
            normalized.append(token)
            idx += 1
            continue

        candidate = raw_argv[idx + 1]
        if not candidate.startswith("-"):
            normalized.append(token)
            idx += 1
            continue

        try:
            normalized_id = coerce_record_id(candidate)
        except ValueError:
            normalized.append(token)
            idx += 1
            continue

        normalized.append(f"--parent-memory={normalized_id}")
        idx += 2
    return normalized


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse installed `kapy` CLI arguments.

    `config_base` is no longer configurable on the command line; the process
    always uses `Config()` so the default/env-backed config root stays
    consistent with the rest of the runtime. When `--in-channel` is omitted,
    one-shot runs get a fresh `direct/<random>` channel so separate CLI
    invocations do not accidentally share history.
    One-shot prompts default to detached `--async` mode unless `--wait` is
    supplied. Detached launches re-exec the same CLI in a child process, so
    they are only valid for one-shot prompts.
    """

    parser = argparse.ArgumentParser(
        prog="kapy",
        description="One-shot CLI wrapper around k.agent.core.agent_run.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Required one-shot prompt.",
    )
    parser.add_argument(
        "--in-channel",
        default=None,
        help=(
            "Event.in_channel used for prompts sent through this CLI. "
            "Defaults to a fresh direct/<random> channel per invocation."
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
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--async",
        action="store_const",
        const="async",
        dest="execution_mode",
        help=(
            "Launch one detached child `kapy` process for the prompt, then "
            "emit the child pid, memory id, and logfile path as JSON."
        ),
    )
    mode_group.add_argument(
        "--wait",
        action="store_const",
        const="wait",
        dest="execution_mode",
        help="Run the prompt in the foreground and wait for completion.",
    )
    parser.add_argument(
        "--reserved-memory-created-at-ms",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(_normalize_parent_memory_flag_values(argv))


def _agent_run_model_from_config(config: Config) -> Model:
    """Build the `agent_run` model from `<config_base>/config.toml`.

    `kapy` uses `OpenAIChatModel`, so `model_name` must be an OpenAI model id
    such as `gpt-5.2`. Optional TOML `openai_api_key` and `openai_base_url`
    values override the default OpenAI provider resolution for CLI runs; when
    omitted, the provider still falls back to environment/default behavior.
    """

    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    file_config = load_kapybara_toml_config(config.config_base)
    return OpenAIChatModel(
        file_config.agent_run.model_name,
        provider=OpenAIProvider(
            api_key=file_config.agent_run.openai_api_key,
            base_url=file_config.agent_run.openai_base_url,
        ),
    )


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


def _resolve_cli_in_channel(in_channel: str | None) -> str:
    """Return a CLI `in_channel`, generating a fresh default when omitted.

    One-shot `kapy` runs should not share history by default, so omitted
    `--in-channel` generates a fresh `direct/<random>` path per invocation.
    """

    if in_channel is not None:
        return in_channel
    return f"direct/{uuid.uuid4().hex}"


def _resolve_cli_contacts(contacts: list[str] | None) -> list[str]:
    """Resolve CLI contacts without injecting synthetic defaults.

    `kapy` should preserve the caller-supplied contact set exactly. Omitting
    `--contact` now means "run without contacts" instead of manufacturing a
    placeholder contact.
    """

    return list(contacts or [])


def _detached_log_path(config_base: str | Path) -> Path:
    """Return a new logfile path for a detached CLI child run.

    The log lives under `<config_base>/logs/kapy/` so detached runs keep their
    diagnostics next to the config and memory tree they operate on.
    """

    logs_dir = Path(config_base).expanduser().resolve() / "logs" / "kapy"
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    return logs_dir / f"kapy_{timestamp}_{suffix}.log"


def _write_stdout_json(payload: str | BaseModel) -> None:
    """Write exactly one JSON value to stdout plus a trailing newline.

    `kapy` stdout is machine-facing. Foreground runs already produce the
    persisted record JSON string, while detached launches emit a modeled
    metadata object via this helper.
    """

    if isinstance(payload, str):
        text = payload
    else:
        text = payload.model_dump_json()
    sys.stdout.write(text)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _child_cli_argv(argv: Sequence[str] | None) -> list[str]:
    """Return the current CLI argv with detached-launch flags removed.

    Detached mode works by re-executing the same CLI in a child process. The
    child must see the original prompt/options but not the parent-facing mode
    flags, otherwise default async mode would recurse indefinitely.
    """

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    child_argv: list[str] = []
    skip_next = False
    for arg in raw_argv:
        if skip_next:
            skip_next = False
            continue
        if arg in {"--async", "--wait"}:
            continue
        if arg == "--reserved-memory-created-at-ms":
            skip_next = True
            continue
        if arg.startswith("--reserved-memory-created-at-ms="):
            continue
        child_argv.append(arg)
    return child_argv


def _reserved_memory_created_at() -> datetime:
    """Return a reserved local timestamp for one run's final memory record."""

    return datetime.now()


def _created_at_from_millis(millis: int) -> datetime:
    """Decode local naive datetime from a POSIX-millisecond timestamp."""

    return datetime.fromtimestamp(millis / 1000)


@contextmanager
def _override_agent_now(reserved_created_at: datetime | None):
    """Temporarily force `agent.py`'s reserved run timestamp when provided."""

    if reserved_created_at is None:
        yield
        return

    original_datetime = agent_module.datetime

    class _ReservedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return reserved_created_at
            return original_datetime.now(tz)

    agent_module.datetime = _ReservedDateTime
    try:
        yield
    finally:
        agent_module.datetime = original_datetime


def _spawn_detached_cli_run(
    *,
    argv: Sequence[str] | None,
    config: Config,
) -> DetachedCliRunMetadata:
    """Launch a detached one-shot `kapy` child and return its stdout metadata.

    Side effects:
    - Creates `<config_base>/logs/kapy/` if needed.
    - Spawns a new process session whose stdio is redirected to the logfile.
    - Forces `K_CONFIG_BASE` in the child environment to the resolved
      `Config.config_base` so the child observes the same skills and memories
      even when the parent inherited a different shell default.
    """

    reserved_created_at = _reserved_memory_created_at()
    reserved_created_at_ms = int(reserved_created_at.timestamp() * 1000)
    reserved_memory_id = memory_record_id_from_created_at(reserved_created_at)
    child_argv = _child_cli_argv(argv)
    log_path = _detached_log_path(config.config_base)
    env = os.environ.copy()
    env["K_CONFIG_BASE"] = str(config.config_base)
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "k.agent.core.run",
                "--wait",
                f"--reserved-memory-created-at-ms={reserved_created_at_ms}",
                *child_argv,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    return DetachedCliRunMetadata(
        pid=process.pid,
        memory_id=reserved_memory_id,
        logfile=log_path,
    )


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
    The existence probe intentionally prefers `MemoryStore.contains_id()` so
    repeated polling does not force full record loads plus link repair.

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
            # compatibility hook here; the subsequent existence probe must observe
            # the latest on-disk state even when this store keeps no derived
            # indexes.
            memory_store.refresh()
            missing = [
                parent_id
                for parent_id in parent_memories
                if not memory_store.contains_id(parent_id)
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
    reserved_memory_created_at: datetime | None = None,
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
    resolved_in_channel = _resolve_cli_in_channel(in_channel)
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
    with _override_agent_now(reserved_memory_created_at):
        mem = await agent_run(
            model=resolved_model,
            config=config,
            memory_store=memory_store,
            instruct=event,
            parent_memories=resolved_parent_memories,
        )
    memory_store.append(mem)
    return mem.dump_compated()


async def main(argv: list[str] | None = None) -> None:
    """Async CLI entrypoint for the installed `kapy` console script.

    A prompt is required because the old REPL mode was removed. One-shot
    prompts default to detached `--async` mode unless `--wait` is supplied.
    Detached launches exit early after spawning a child and emit the child pid,
    reserved memory id, and logfile path as JSON.
    """

    args = _parse_cli_args(argv)
    if args.prompt is None:
        raise SystemExit("kapy requires a prompt; REPL mode was removed")

    config = Config()
    _configure_cli_logfire(config.config_base)
    reserved_memory_created_at = None
    if args.reserved_memory_created_at_ms is not None:
        reserved_memory_created_at = _created_at_from_millis(
            args.reserved_memory_created_at_ms
        )

    should_run_async = args.execution_mode != "wait"
    if should_run_async:
        _write_stdout_json(_spawn_detached_cli_run(argv=argv, config=config))
        return

    memory_store = FolderMemoryStore(
        root=memory_root_from_config_base(config.config_base),
    )
    model = _agent_run_model_from_config(config)
    contacts = _resolve_cli_contacts(args.contacts)
    parent_memories = _normalize_parent_memory_ids(args.parent_memories)

    _write_stdout_json(
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
            reserved_memory_created_at=reserved_memory_created_at,
        )
    )


def cli() -> None:
    """Synchronous console-script shim for `kapy`."""

    asyncio.run(main())


if __name__ == "__main__":
    cli()
