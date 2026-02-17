"""Tool implementations and runtime dependencies for the agent.

This module is intentionally limited to tool implementations.
The deps container is defined in `k.agent.core.agent.MyDeps`, and the runtime
entrypoint is `k.agent.core.agent.agent_run`.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Concatenate, Protocol

from pydantic_ai import RunContext

from k.io_helpers.shell import (
    NextResult,
    ShellSessionInfo,
    ShellSessionManager,
    ShellSessionOptions,
)
from k.runner_helpers.basic_os import BasicOSHelper, single_quote

_BASH_STDIO_TOKEN_LIMIT = 16000
_CL100K_BASE_ENCODING: Any | None = None
_BASH_COUNTDOWN_SYSTEM_MSG = "You've been working for a while. Pause to send a brief progress update to the originating event channel, then continue working."


def _cl100k_base_token_len(text: str) -> int:
    """Count tokens using `tiktoken`'s `cl100k_base`.

    Used to keep bash tool responses within a predictable token budget so the
    agent doesn't accidentally ingest huge stdout/stderr payloads.
    """

    if not text:
        return 0

    global _CL100K_BASE_ENCODING
    if _CL100K_BASE_ENCODING is None:
        import tiktoken

        _CL100K_BASE_ENCODING = tiktoken.get_encoding("cl100k_base")

    # `tiktoken` returns a list of token ids; its length is the token count.
    return len(_CL100K_BASE_ENCODING.encode(text))


def _append_system_msg(existing: str | None, extra: str) -> str:
    if existing:
        return f"{existing}\n{extra}"
    return extra


def _validate_timeout_seconds(timeout_seconds: float | None) -> float | None:
    """Return timeout when valid; reject non-positive values."""

    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError(f"timeout_seconds must be > 0; got {timeout_seconds}")
    return timeout_seconds


class ShellToolDeps(Protocol):
    """Structural deps contract required by the bash-like tools in this module."""

    basic_os_helper: BasicOSHelper
    shell_manager: ShellSessionManager

    bash_cmd_history: list[str]
    stuck_warning: int
    stuck_warning_limit: int
    count_down: int


def bash_countdown_tool[**P, R](
    fn: Callable[Concatenate[RunContext[ShellToolDeps], P], Awaitable[R]],
) -> Callable[Concatenate[RunContext[ShellToolDeps], P], Awaitable[R]]:
    """Decorator that decrements `ctx.deps.count_down` and appends a reminder.

    This is applied to bash-like tools (tools that may return `BashEvent`) so the
    agent periodically gets nudged to post a progress update.
    """

    @functools.wraps(fn)
    async def wrapper(
        ctx: RunContext[ShellToolDeps], *args: P.args, **kwargs: P.kwargs
    ) -> R:
        result = await fn(ctx, *args, **kwargs)

        deps = ctx.deps
        should_append = deps.count_down == 1
        if deps.count_down > 0:
            deps.count_down -= 1

        if should_append and isinstance(result, BashEvent):
            result.system_msg = _append_system_msg(
                result.system_msg, _BASH_COUNTDOWN_SYSTEM_MSG
            )

        return result

    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper


@dataclass(slots=True)
class BashEvent:
    session_id: str
    stdout: str
    stderr: str
    exit_code: int | None = None
    active_sessions: list[ShellSessionInfo] = field(default_factory=list)
    system_msg: str | None = None

    @classmethod
    def new(
        cls,
        session_id: str,
        tpl: NextResult,
        *,
        all_active_sessions: list[ShellSessionInfo],
        system_msg: str | None = None,
    ) -> BashEvent:
        stdout, stderr, exit_code = tpl
        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")

        combined_stdio = stdout_text + stderr_text
        if (
            len(combined_stdio) > _BASH_STDIO_TOKEN_LIMIT
            and _cl100k_base_token_len(combined_stdio) > _BASH_STDIO_TOKEN_LIMIT
        ):
            stdout_text = ""
            stderr_text = ""
            too_long_msg = "The stdout/stderr is too long, please decrease the output size if possiable, or dump to a /tmp file before consume."
            system_msg = (
                too_long_msg if system_msg is None else f"{system_msg}\n{too_long_msg}"
            )

        return BashEvent(
            session_id=session_id,
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=exit_code,
            active_sessions=all_active_sessions,
            system_msg=system_msg,
        )


async def _bash_impl(
    ctx: RunContext[ShellToolDeps],
    text: str,
    *,
    timeout_seconds: float | None = None,
) -> BashEvent:
    """
    Start a new bash session with the given commands text.

    Args:
        text: The initial commands text to run in the bash session. The commands can be one line or multiple lines.
        timeout_seconds: Optional wait timeout override for the initial wait.
    """
    timeout_seconds = _validate_timeout_seconds(timeout_seconds)

    options = (
        ShellSessionOptions(timeout_seconds=timeout_seconds)
        if timeout_seconds is not None
        else None
    )

    session_id = await ctx.deps.shell_manager.new_shell(
        ctx.deps.basic_os_helper.command(text),
        options=options,
        desc=text[:30] + ("..." if len(text) > 30 else ""),
    )
    text = text.strip()
    system_msg = None
    if ctx.deps.bash_cmd_history and ctx.deps.bash_cmd_history[-1] == text:
        if ctx.deps.stuck_warning >= ctx.deps.stuck_warning_limit:
            system_msg = (
                "You seems to be stuck. You MUST finish with kind: `stuck` right now."
            )
        else:
            system_msg = "You are using the same bash command as the last time. If you get stuck, finish with kind: `stuck`."
        ctx.deps.stuck_warning += 1
    ctx.deps.bash_cmd_history.append(text.strip())
    res = await ctx.deps.shell_manager.next(session_id)
    active_sessions = await ctx.deps.shell_manager.list_sessions()
    return BashEvent.new(
        session_id, res, all_active_sessions=active_sessions, system_msg=system_msg
    )


@bash_countdown_tool
async def bash(
    ctx: RunContext[ShellToolDeps], text: str, timeout_seconds: float | None = None
) -> BashEvent:
    """
    Start a new bash session and run initial commands.

    Args:
        text: Initial command text (single-line or multi-line).
        timeout_seconds: Optional wait timeout override for this session.
    """
    return await _bash_impl(ctx, text, timeout_seconds=timeout_seconds)


@bash_countdown_tool
async def bash_input(
    ctx: RunContext[ShellToolDeps],
    session_id: str,
    text: str,
    timeout_seconds: float | None = None,
) -> BashEvent | str:
    """
    Send stdin to a bash session.

    Args:
        session_id: The session id returned by `bash`.
        text: The stdin text to send to the bash session, usually ended with a newline.
        timeout_seconds: Optional timeout override for this wait.
    """
    timeout_seconds = _validate_timeout_seconds(timeout_seconds)

    try:
        res = await ctx.deps.shell_manager.next(
            session_id, stdin=text.encode(), timeout_seconds=timeout_seconds
        )
    except KeyError:
        return f"Unknown session id: {session_id}. Start a new session with bash."
    active_sessions = await ctx.deps.shell_manager.list_sessions()
    return BashEvent.new(session_id, res, all_active_sessions=active_sessions)


@bash_countdown_tool
async def bash_wait(
    ctx: RunContext[ShellToolDeps],
    session_id: str,
    timeout_seconds: float | None = None,
) -> BashEvent | str:
    """
    Wait for the next output from a bash session.

    Args:
        session_id: The session id returned by `bash`.
        timeout_seconds: Optional timeout override for this wait.
    """
    timeout_seconds = _validate_timeout_seconds(timeout_seconds)

    try:
        res = await ctx.deps.shell_manager.next(
            session_id, timeout_seconds=timeout_seconds
        )
    except KeyError:
        return f"Unknown session id: {session_id}. Start a new session with bash."
    active_sessions = await ctx.deps.shell_manager.list_sessions()
    return BashEvent.new(session_id, res, all_active_sessions=active_sessions)


async def bash_interrupt(ctx: RunContext[ShellToolDeps], session_id: str) -> str:
    """
    Interrupt a bash session. If the session is already ended, do nothing.
    """

    try:
        await ctx.deps.shell_manager.interrupt(session_id)
    except KeyError:
        return f"Unknown session id: {session_id}. Ignored."
    return "Session ended."


@bash_countdown_tool
async def edit_file(
    ctx: RunContext[ShellToolDeps],
    filename: str,
    old_content: str,
    new_content: str,
    start_line: int | None = None,
) -> BashEvent:
    """Edit a file by replacing a known slice of lines.

    Args:
        filename: Target file path (relative or absolute, cannot use `~`).
        old_content: The exact content expected at `start_line` (normalized for newlines).
        new_content: The replacement content.
        start_line: 1-based line number where `old_content` is expected to start, or None to auto-detect.
    """

    return await _bash_impl(
        ctx,
        f"python3 ~/skills/meta/edit-file/edit.py --filename {single_quote(filename)} --old-content {single_quote(old_content)} --new-content {single_quote(new_content)} "
        + (
            f"--start-line {single_quote(str(start_line))}"
            if start_line is not None
            else ""
        ),
    )
