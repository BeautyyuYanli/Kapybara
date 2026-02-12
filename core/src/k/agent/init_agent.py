from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from pydantic_ai import (
    Agent,
    ModelMessage,
    ModelRetry,
    RunContext,
    ToolOutput,
    ToolReturnPart,
)
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.models import KnownModelName, Model
from rich import print

from k.agent.memory.compactor import run_compaction
from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config
from k.io_helpers.shell import NextResult, ShellSessionInfo, ShellSessionManager
from k.runner_helpers.basic_os import BasicOSHelper, single_quote

_BASH_STDIO_TOKEN_LIMIT = 8000
_CL100K_BASE_ENCODING: Any | None = None


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


@dataclass()
class MyDeps:
    """Dependencies for the agent run.

    Lifecycle:
        `MyDeps` owns a `ShellSessionManager` which may keep subprocesses alive
        across multiple tool calls. Always close it when the deps are no longer
        needed (prefer `async with MyDeps(...)`).
    """

    config: Config
    memory_storage: FolderMemoryStore
    memory_parents: list[str]
    start_event: Event | None = None
    bash_cmd_history: list[str] = field(default_factory=list)
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
            too_long_msg = "The stdout/stderr is too long, please dump to a /tmp file before consume."
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


async def bash(ctx: RunContext[MyDeps], text: str) -> BashEvent:
    """
    Start a new bash session with the given commands text.

    Args:
        text: The initial commands text to run in the bash session. The commands can be one line or multiple lines.
    """
    session_id = await ctx.deps.shell_manager.new_shell(
        ctx.deps.basic_os_helper.command(text),
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


async def bash_input(
    ctx: RunContext[MyDeps], session_id: str, text: str
) -> BashEvent | str:
    """
    Send stdin to a bash session.

    Args:
        session_id: The session id returned by `bash`.
        text: The stdin text to send to the bash session, usually ended with a newline.
    """
    try:
        res = await ctx.deps.shell_manager.next(session_id, stdin=text.encode())
    except KeyError:
        return f"Unknown session id: {session_id}. Start a new session with bash."
    active_sessions = await ctx.deps.shell_manager.list_sessions()
    return BashEvent.new(session_id, res, all_active_sessions=active_sessions)


async def bash_wait(ctx: RunContext[MyDeps], session_id: str) -> BashEvent | str:
    """
    Wait for the next output from a bash session.
    """
    try:
        res = await ctx.deps.shell_manager.next(session_id)
    except KeyError:
        return f"Unknown session id: {session_id}. Start a new session with bash."
    active_sessions = await ctx.deps.shell_manager.list_sessions()
    return BashEvent.new(session_id, res, all_active_sessions=active_sessions)


async def bash_interrupt(ctx: RunContext[MyDeps], session_id: str) -> str:
    """
    Interrupt a bash session. If the session is already ended, do nothing.
    """
    try:
        await ctx.deps.shell_manager.interrupt(session_id)
    except KeyError:
        return f"Unknown session id: {session_id}. Ignored."
    return "Session ended."


async def edit_file(
    ctx: RunContext[MyDeps],
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
    return await bash(
        ctx,
        f"python3 ~/skills/meta/edit-file/edit.py --filename {single_quote(filename)} --old-content {single_quote(old_content)} --new-content {single_quote(new_content)} "
        + (
            f"--start-line {single_quote(str(start_line))}"
            if start_line is not None
            else ""
        ),
    )


async def fork(
    ctx: RunContext[MyDeps],
    instruct: str,
    # inject_memories: list[str] | None = None,
) -> str:
    """Run `instruct` in a forked agent run. The fork reuses the current conversation and memory context.

    Returns a short status string; on success it includes the forked run's
    compacted memory record.

    `fork` can not be used as the first tool call.
    """

    parent_mems = []
    # parent_mems = (
    #     inject_memories if inject_memories else []
    # )
    if len(ctx.messages) == 1:
        raise ModelRetry("Cannot fork as the first message.")
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
            instruct=[
                "You are the forked agent to complete only the following instruct, ignoring the previous ones.\nInstruction: ",
                instruct,
            ],
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


class Event(BaseModel):
    kind: Literal["instruct", "response", "stuck"]
    id_: str = Field(default_factory=lambda: str(uuid4()))
    text: str


HandoffKind = Literal["response", "stuck"]


class HandoffEvent(Event):
    kind: HandoffKind


async def handoff(
    ctx: RunContext[MyDeps], kind: Literal["response", "stuck"], text: str
) -> HandoffEvent:
    """Finish the loop.

    Args:
        kind: The kind of handoff event. The value can be:
              "response": The agent has completed.
              "stuck": The agent is unable to proceed.
        text: The related text message. When in the case of
              - response: The text to response.
              - stuck: The text describing the situation
    """
    if ctx.deps.start_event is None:
        id_ = str(uuid4())
    else:
        id_ = ctx.deps.start_event.id_
    return HandoffEvent(kind=kind, id_=id_, text=text)


bash_tool_prompt = """
<BashInstruction>
You have access to a Linux machine via a bash shell, exposed through these tools:
- `bash`: start a new session and run initial commands
- `bash_input`: send more input to an existing session
- `bash_wait`: wait for an existing session to produce more output / finish
- `bash_interrupt`: interrupt an existing session

Session model:
- `bash` always returns a `session_id`. Use that `session_id` for follow-up calls.
- If `exit_code` is `null`, the session is still running.
- If `exit_code` is an `int`, the session has finished and is closed.

Operating rules:
- Do not run meaningless commands (e.g. `true`, `echo ...`) unless they are part of a real workflow.
- If a command needs time, do not skip it—keep calling `bash_wait` until `exit_code` becomes non-null (or interrupt if necessary).
- If a command outputs a lot, redirect it to a file (e.g. under `/tmp`) and then read only the relevant parts.
- You do not have root access. If a command would require root, return the command(s) instead of trying to run them.
</BashInstruction>
"""

agent: Agent[MyDeps, HandoffEvent] = Agent(
    system_prompt=[
        bash_tool_prompt,
        "<General>\n"
        "Role: You are an agent who can use the provided tools and skills in `~/skills/`.\n"
        "\n"
        "How to operate:\n"
        "- Determine the user's intent first (often clarified by the most recent memories). Memories are ordered oldest → newest.\n"
        "- If the intent is ambiguous, do not start work; finish quickly and ask for clarification.\n"
        "- Usually use the `retrieve-memory` skill as the first action to gather more context (e.g. the user references prior work, you need to reuse earlier outputs, or current intent depends on older memories).\n"
        "- Usually use `file-search` skill under `~/skills/` as the second action to see whether a relevant skill already exists.\n"
        "\n"
        "Skills:\n"
        "- Actively gather missing information using available `*-search` skills (e.g. `web-search`, `file-search`) instead of guessing.\n"
        "- Prefer `web-fetch` to fetch readable page text instead of downloading raw HTML (only fall back to raw HTML when necessary).\n"
        "- Assume required environment variables for existing skills are already set; do not double-check them.\n"
        "- If a required environment variable is missing, ask the user to add it to `~/.env`.\n"
        "- Use the `create-skill` tool to create new skills when needed.\n"
        "\n"
        "Scripting:\n"
        "- For one-off Python scripts, prefer inline deps in-file (PEP 723, `# /// script`) to keep scripts reproducible/self-contained; use `execute-code` when appropriate.\n"
        "\n"
        "Software & storage:\n"
        "- You may install software into `/App` or via a user-space package manager, then create skills to use it.\n"
        "- Use `/tmp` for temporary storage.\n"
        "\n"
        "Completion:\n"
        "- When done, call the `finish` tool to end the loop.\n"
        "- If you cannot proceed, call `finish` with kind: `stuck` (even if the work is unfinished).\n",
        "\n</General>",
    ],
    tools=[bash, bash_input, bash_wait, bash_interrupt, edit_file, fork],
    deps_type=MyDeps,
    output_type=ToolOutput(handoff, name="finish"),
)  # type: ignore


def _concat_skills_md(base_path: str | Path) -> str:
    """Scan <base_path>/skills/{core,meta}/*/SKILLS.md and concatenate contents.

    Returns a single string which is the concatenation of all found SKILLS.md files,
    separated by clear delimiters.
    """
    base_path = Path(base_path).expanduser().resolve()
    skills_root = base_path / "skills"

    chunks: list[str] = []

    for group in ("core", "meta"):
        group_root = skills_root / group
        if not group_root.exists():
            continue

        for md in sorted(
            group_root.glob("*/SKILLS.md"), key=lambda p: (p.parent.name, str(p))
        ):
            content = md.read_text()
            chunks.append(
                "\n".join(
                    [
                        f"# ===== ~/skills/{group}/{md.parent.name}/SKILLS.md =====",
                        content.rstrip(),
                        "",
                    ]
                )
            )

    return "\n".join(chunks).rstrip() + "\n"


@agent.system_prompt
def concat_skills_prompt(ctx: RunContext[MyDeps]) -> str:
    base_path = ctx.deps.config.fs_base
    skills_md = _concat_skills_md(base_path)
    return f"<BasicSkills>{skills_md}</BasicSkills>"


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
    instruct: list[UserContent],
    message_history: Sequence[ModelMessage] | None = None,
    parent_memories: list[str] | None = None,
) -> tuple[HandoffEvent, MemoryRecord]:
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
    ) as my_deps:
        res = await agent.run(
            model=model,
            deps=my_deps,
            user_prompt=(
                f"<Memory>{memory_string}</Memory>\n" if parent_memories else "",
                f"<System>Now: {datetime.now()}</System>\n",
                *instruct,
            ),
            message_history=message_history,
        )
    msgs: list[ModelRequest | ModelResponse] = res.new_messages()
    msgs = _strip_history(msgs, instruct)
    compacted = await run_compaction(
        model=model,
        detailed=msgs,
    )
    mem = MemoryRecord(
        raw_pair=(
            "\n".join((x if isinstance(x, str) else str(x)) for x in instruct),
            res.output.text,
        ),
        compacted=compacted,
        parents=parent_memories,
        detailed=msgs,
    )
    return res.output, mem


async def main():

    config = Config()  # type: ignore
    model = "openai:gpt-5.2"
    mem_store = FolderMemoryStore(
        root=config.fs_base / "memories",
    )
    while True:
        instruct = input("\nEnter your instruction (or 'exit' to quit): ")
        if instruct.lower() in {"exit", "quit"}:
            print("Exiting the agent loop.")
            break
        latest_mem = mem_store.get_latest()
        output, mem = await agent_run(
            model,
            config,
            mem_store,
            [instruct],
            parent_memories=[latest_mem] if latest_mem else [],
        )
        print(output.text)
        mem_store.append(mem)
        print(mem.compacted)


if __name__ == "__main__":
    import anyio
    import logfire
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv("core/.env")

    logfire.configure()
    logfire.instrument_pydantic_ai()
    anyio.run(main)
