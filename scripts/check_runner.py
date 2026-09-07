"""Check the real Runner/State combination with one small live model response."""

import asyncio
import json
import os
from uuid import UUID, uuid4

import httpx2
import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kapy.agent import AgentPayloadStore, Runner, RunnerConfig
from kapy.rpc import JsonObject, JsonValue
from kapy.state import SessionService, SessionSpec, migrate


class NoMachine:
    async def call(
        self,
        machine_id: str,
        method: str,
        params: JsonObject,
        *,
        timeout: float = 60,  # noqa: ASYNC109 - MachineCaller contract
    ) -> JsonValue:
        raise AssertionError("This acceptance session has no execution machine")


async def no_external_wait(session_id: UUID, channels: tuple[UUID, ...]) -> None:
    if channels:
        raise PermissionError("This acceptance session has no external channel grants")


async def check() -> None:
    database_url = os.environ["KAPY_DATABASE_URL"]
    valkey_url = os.environ["KAPY_VALKEY_URL"]
    schema = "kapy_runner_" + uuid4().hex
    marker = "KAPY_RUNNER_" + uuid4().hex
    try:
        await migrate(database_url, schema=schema)
        async with (
            AsyncConnectionPool(database_url, open=False, min_size=1, max_size=2) as pool,
            httpx2.AsyncClient(trust_env=False) as http,
            asyncio.timeout(120),
        ):
            payloads = AgentPayloadStore(pool, schema=schema)
            await payloads.initialize()
            runner = Runner(
                RunnerConfig(
                    base_url=os.environ["OPENAI_BASE_URL"],
                    api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
                    model=os.environ["OPENAI_MODEL"],
                    context_window_tokens=1_050_000,
                    max_output_tokens=256,
                ),
                NoMachine(),
                http_client=http,
                payload_store=payloads,
                authorize_wait=no_external_wait,
            )

            def service() -> SessionService:
                return SessionService(
                    database_url=database_url,
                    valkey_url=valkey_url,
                    runner=runner,
                    schema=schema,
                    namespace=schema,
                )

            async with service() as state:
                created = await state.create_session(
                    SessionSpec(
                        title="Live Runner acceptance",
                        machine_ids=(),
                        default_machine_id=None,
                        config={},
                        initial_state=runner.initial_state(
                            instructions="Follow the user's requested reply format exactly.",
                            skills=(),
                        ),
                    ),
                    request_id=uuid4(),
                    input=f"Reply with exactly {marker}. No tools are needed.",
                )
                cursor = None
                finished = False
                while not finished:
                    page = await state.read_output(created.session.id, after=cursor, wait_seconds=1)
                    cursor = page.next_cursor
                    for record in page.items:
                        if record.kind == "error":
                            raise AssertionError("Runner failed: " + json.dumps(record.data))
                        if record.kind == "waiting" and isinstance(record.data, dict):
                            request_ids = record.data.get("request_ids", [])
                            if (
                                isinstance(request_ids, list)
                                and str(created.submission.request_id) in request_ids
                            ):
                                if record.data.get("output") != marker:
                                    raise AssertionError("Live Runner output did not match input")
                                finished = True

            async with service() as reopened:
                page = await reopened.read_output(created.session.id, limit=200)
                responses = [item for item in page.items if item.kind == "model_response"]
                usage = [
                    item.data.get("usage") for item in responses if isinstance(item.data, dict)
                ]
                reported_input = [
                    item.get("input_tokens") for item in usage if isinstance(item, dict)
                ]
                if not responses or not any(
                    isinstance(count, int) and count > 0 for count in reported_input
                ):
                    raise AssertionError("Complete model response and API usage were not replayed")
                print(
                    json.dumps(
                        {
                            "model": os.environ["OPENAI_MODEL"],
                            "reply_matches_input": True,
                            "responses_replayed_after_reopen": len(responses),
                            "api_usage": usage,
                        }
                    )
                )
    finally:
        async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


if __name__ == "__main__":
    asyncio.run(check())
