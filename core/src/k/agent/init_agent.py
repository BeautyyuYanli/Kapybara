from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelMessage, RunContext, ToolOutput
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import KnownModelName, Model
from rich import print

from k.agent.memory.compactor import run_compaction
from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config
from k.io_helpers.shell import NextResult, ShellSessionInfo, ShellSessionManager
from k.runner_helpers.basic_os import BasicOSHelper, single_quote


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
        return BashEvent(
            session_id=session_id,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
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
        f"python3 ~/skills/meta/edit_file/edit.py --filename {single_quote(filename)} --old-content {single_quote(old_content)} --new-content {single_quote(new_content)} "
        + (
            f"--start-line {single_quote(str(start_line))}"
            if start_line is not None
            else ""
        ),
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
- You do not have root access. If a command would require root, return the command(s) instead of trying to run them.
</BashInstruction>
"""

agent: Agent[MyDeps, HandoffEvent] = Agent(
    system_prompt=[
        bash_tool_prompt,
        "<General>\n"
        "Role: You can use the provided tools and skills in `~/skills/`.\n"
        "\n"
        "How to operate:\n"
        "- Determine the user's intent first (often clarified by the most recent memories). Memories are ordered oldest → newest.\n"
        "- If the intent is ambiguous, do not start work; finish quickly and ask for clarification.\n"
        "- Usually use the `retrieve_memory` skill as the first action to gather more context (e.g. the user references prior work, you need to reuse earlier outputs, or current intent depends on older memories).\n"
        "- Usually use `file_search` skill under `~/skills/` as the second action to see whether a relevant skill already exists.\n"
        "\n"
        "Skills:\n"
        "- Actively gather missing information using available `*_search` skills (e.g. `web_search`, `file_search`) instead of guessing.\n"
        "- Prefer `web_fetch` to fetch readable page text instead of downloading raw HTML (only fall back to raw HTML when necessary).\n"
        "- Assume required environment variables for existing skills are already set; do not double-check them.\n"
        "- If a required environment variable is missing, ask the user to add it to `~/.env`.\n"
        "- Use the `create_skill` tool to create new skills when needed.\n"
        "\n"
        "Scripting:\n"
        "- For one-off Python scripts, prefer inline deps in-file (PEP 723, `# /// script`) to keep scripts reproducible/self-contained; use `execute_code` when appropriate.\n"
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
    tools=[bash, bash_input, bash_wait, bash_interrupt, edit_file],
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


def _strip_history(msgs: list[ModelRequest | ModelResponse], instruct: str):
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
):
    recent_mem = set(parent_memories)
    all_mem = set(parent_memories)
    for mem in parent_memories:
        recent_mem.union(memory_store.get_ancestors(mem, level=3))
        all_mem.union(memory_store.get_ancestors(mem, level=10))

    all_mem_rec = memory_store.get_by_ids(all_mem)
    return all_mem_rec, recent_mem


async def agent_run(
    model: Model | KnownModelName,
    config: Config,
    memory_store: FolderMemoryStore,
    instruct: str,
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

    if message_history:
        message_history = copy(list(message_history))
        user_prompt_part = UserPromptPart(
            f"<Memory>{memory_string}</Memory>\n" if parent_memories else f"{instruct}",
        )
        if not isinstance(message_history[-1], ModelRequest):
            message_history.append(ModelRequest(parts=[user_prompt_part]))
        else:
            message_history[-1].parts = copy(list(message_history[-1].parts))
            message_history[-1].parts.append(user_prompt_part)

    async with MyDeps(config=config, memory_storage=memory_store) as my_deps:
        res = await agent.run(
            model=model,
            deps=my_deps,
            user_prompt=(
                f"<Memory>{memory_string}</Memory>\n" if parent_memories else "",
                f"<System>Now: {datetime.now()}</System>\n",
                f"{instruct}",
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
        raw_pair=(instruct, res.output.text),
        compacted=compacted,
        parents=parent_memories,
        detailed=msgs,
    )
    return res.output, mem


async def main():
    from rich import print

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
            instruct,
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
