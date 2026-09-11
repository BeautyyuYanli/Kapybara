"""Exercise tmpv2 against PostgreSQL, Valkey and the configured live Responses API.

Run explicitly inside the Compose runtime. This makes several billable model
requests and leaves one session in KAPY_DATABASE_SCHEMA for inspection. Tables
must already be initialized with ``kapy db upgrade``.
Process fixtures are temporary. No legacy control server or Telegram is involved.
"""

import asyncio
import json
import os
import sys
from contextlib import aclosing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from valkey.asyncio import Valkey

from kapy.tmpv2.agent_output import AgentOutputService
from kapy.tmpv2.agent_runner import HistoryMessage, MessageCommitted, TextDelta, open_runner
from kapy.tmpv2.agent_runner.repository import AgentRepository
from kapy.tmpv2.control.models import CreateModel, CreateProvider, ModelService
from kapy.tmpv2.control.sessions import CreateSession, SessionService, UpdateSession
from kapy.tmpv2.file_transfer import read_file, write_file
from kapy.tmpv2.processes import ProcessSpec, open_process_manager


async def read_history(
    factory: async_sessionmaker[AsyncSession], session_id: UUID
) -> tuple[HistoryMessage, ...]:
    async with factory.begin() as db:
        return await AgentRepository(db).read_history_entries(session_id)


async def check(
    factory: async_sessionmaker[AsyncSession], client: Valkey, agent: Agent[None, str]
) -> None:
    marker = "TMPV2_" + uuid4().hex
    prefix = os.environ.get("KAPY_VALKEY_NAMESPACE", "kapy_tmpv2") + ":agent-output"
    sessions = SessionService(
        factory, output_service=AgentOutputService(client, channel_prefix=prefix)
    )
    models = ModelService(factory)
    provider = await models.create_provider(
        CreateProvider(
            name="live-check",
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="pydantic_ai.models.openai:OpenAIResponsesModel",
            api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
            base_url=os.environ["OPENAI_BASE_URL"],
        )
    )
    model = await models.create_model(
        CreateModel(
            provider_id=provider.id,
            model_name=os.environ["OPENAI_MODEL"],
            settings={"max_tokens": 2048, "timeout": 90},
        )
    )
    session = await sessions.create_session(
        CreateSession(
            provider_id=provider.id,
            model_name=model.model_name,
            # Keep the initial phase for the explicit manual-compaction check below.
            compaction_threshold_tokens=2**31 - 1,
        )
    )
    session_id = session.id
    committed: list[HistoryMessage] = []
    deltas: list[TextDelta] = []
    changed = asyncio.Event()
    tool_calls = 0

    async def observe() -> None:
        async with aclosing(sessions.live(session_id)) as events:
            async for event in events:
                if isinstance(event, MessageCommitted):
                    committed.append(event.message)
                    changed.set()
                else:
                    deltas.append(event)

    with TemporaryDirectory(prefix="kapy-tmpv2-") as directory:
        root = Path(directory)
        process_id = uuid4()
        async with open_process_manager(state_dir=root / "state") as processes:

            @agent.tool_plain
            async def generate_artifact() -> str:
                """Run a process to write an artifact and return its exact verification marker."""
                nonlocal tool_calls
                tool_calls += 1
                assert tool_calls == 1, "The artifact tool must run exactly once"
                await processes.start(
                    process_id,
                    ProcessSpec(
                        argv=(
                            sys.executable,
                            "-c",
                            "import os, pathlib, sys; "
                            "assert 'OPENAI_API_KEY' not in os.environ; "
                            "pathlib.Path('artifact.txt').write_text(sys.argv[1]); "
                            "print(sys.argv[1])",
                            marker,
                        ),
                        cwd=directory,
                    ),
                )
                status = await processes.wait(process_id, timeout=10)
                assert status.state == "exited" and status.exit_code == 0
                output = await processes.read_output(process_id, stream="stdout")
                assert output.eof and output.data.decode().strip() == marker
                async with aclosing(read_file(root / "artifact.txt")) as content:
                    assert await write_file(root / "copy.txt", content) == len(marker.encode())
                async with aclosing(read_file(root / "copy.txt")) as content:
                    assert b"".join([chunk async for chunk in content]).decode() == marker
                return marker

            await sessions.enqueue_input(
                session_id,
                "queued",
                "Call generate_artifact exactly once, then reply with only the marker it returns.",
            )
            async with asyncio.TaskGroup() as tasks:
                observer = tasks.create_task(observe())
                try:
                    async with asyncio.timeout(5):
                        # Readiness is external Valkey state, not a local task event.
                        while (await client.pubsub_numsub(f"{prefix}:{session_id}"))[0][1] != 1:  # noqa: ASYNC110
                            await asyncio.sleep(0.01)
                    print("Checking live model, process tool and streaming output...", flush=True)
                    result = await sessions.start_runner(
                        session_id, agent=agent, realtime_output=True
                    )
                    assert result.finished and result.output is not None
                    assert result.output.strip() == marker and tool_calls == 1
                    history = await read_history(factory, session_id)
                    async with asyncio.timeout(5):
                        while len(committed) < len(history):
                            await changed.wait()
                            changed.clear()
                    assert committed == list(history), "Live DTOs differ from database history"
                finally:
                    observer.cancel()

        # Reopen the process store and confirm completed output survives its owner.
        async with open_process_manager(state_dir=root / "state") as processes:
            output = await processes.read_output(process_id, stream="stdout")
            assert output.data.decode().strip() == marker

        assert deltas and any(event.part_kind == "text" for event in deltas)
        parts: dict[tuple[int, int], str] = {}
        for event in deltas:
            key = (event.response_seq, event.part_index)
            parts[key] = event.text if event.op == "replace" else parts.get(key, "") + event.text
        for (seq, index), content in parts.items():
            part = history[seq].message.parts[index]
            assert isinstance(part, TextPart | ThinkingPart) and content == part.content
        assert any(isinstance(part, ToolCallPart) for row in history for part in row.message.parts)
        assert any(
            isinstance(part, ToolReturnPart) for row in history for part in row.message.parts
        )
        async with aclosing(sessions.live(session_id, after_seq=0)) as events:
            for row in history[1:]:
                assert await anext(events) == MessageCommitted(row)

        print("Checking manual compaction, restart and automatic compaction...", flush=True)
        async with open_runner(session_id, agent=agent, session_factory=factory) as runner:
            await runner.rebuild_context()
            summary = await runner.compact()
            assert summary is not None and summary.text.strip()
            assert summary.last_message_seq == history[-1].seq
        assert await read_history(factory, session_id) == history

        await sessions.enqueue_input(
            session_id,
            "queued",
            "Without calling any tools, reply with only the exact artifact marker from earlier.",
        )
        await sessions.update_session(
            session_id,
            UpdateSession(compaction_replay_turns=0, compaction_threshold_tokens=1),
        )
        resumed = await sessions.start_runner(session_id, agent=agent, realtime_output=False)
        assert resumed.finished and resumed.output is not None
        assert resumed.output.strip() == marker and tool_calls == 1
        final_history = await read_history(factory, session_id)
        assert final_history[: len(history)] == history
        assert len(final_history) == len(history) + 2
        async with factory.begin() as db:
            automatic = await AgentRepository(db).read_latest_compaction(session_id)
        assert automatic is not None and automatic.last_message_seq == final_history[-1].seq
        assert not await sessions.read_inputs(session_id, "queued")
        # Disabled broadcasting still checkpoints; reconnect recovers that durable tail.
        async with aclosing(sessions.live(session_id, after_seq=len(history) - 1)) as events:
            for row in final_history[len(history) :]:
                assert await anext(events) == MessageCommitted(row)
        assert (await client.pubsub_numsub(f"{prefix}:{session_id}"))[0][1] == 0

    print(
        json.dumps(
            {
                "status": "passed",
                "session_id": str(session_id),
                "model": os.environ["OPENAI_MODEL"],
                "history_messages": len(final_history),
                "live_committed_messages": len(committed),
                "text_deltas": sum(event.part_kind == "text" for event in deltas),
                "thinking_deltas": sum(event.part_kind == "thinking" for event in deltas),
                "tool_calls": tool_calls,
                "manual_compaction_seq": summary.last_message_seq,
                "automatic_compaction_seq": automatic.last_message_seq,
            },
            indent=2,
        )
    )


async def main() -> None:
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL"):
        if not os.environ.get(name):
            raise RuntimeError(f"Set {name} in .env before running the live check")
    schema = os.environ.get("KAPY_DATABASE_SCHEMA", "kapy_tmpv2")
    engine = create_async_engine(
        os.environ["KAPY_DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://", 1),
        execution_options={"schema_translate_map": {None: schema}},
    )
    try:
        async with (
            Valkey.from_url(os.environ["KAPY_VALKEY_URL"], socket_connect_timeout=2) as valkey,
            AsyncOpenAI(
                base_url=os.environ["OPENAI_BASE_URL"],
                api_key=os.environ["OPENAI_API_KEY"],
                timeout=90,
                max_retries=0,
            ) as openai,
            OpenAIResponsesModel(
                os.environ["OPENAI_MODEL"], provider=OpenAIProvider(openai_client=openai)
            ) as model,
            asyncio.timeout(480),
        ):
            agent = Agent(model, model_settings={"max_tokens": 2048, "timeout": 90})
            await check(async_sessionmaker(engine, expire_on_commit=False), valkey, agent)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if not Path("/.dockerenv").exists():
        raise RuntimeError("Run this check inside the Compose runtime")
    asyncio.run(main())
