from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest_asyncio
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from valkey.asyncio import Valkey

from kapy.agent import AgentPayloadStore
from kapy.gateway.auth import Authenticator
from kapy.gateway.control import ControlService
from kapy.gateway.machines import MachineRegistry
from kapy.gateway.storage import Metadata, migrate
from kapy.settings import Settings
from kapy.skills import SkillService
from kapy.state import CheckpointWrite, RunnerState, RunResult, SessionService
from kapy.state import migrate as migrate_state

DATABASE = "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy"
VALKEY = "redis://127.0.0.1:56379/0"


class EchoRunner:
    def initial_state(self, *, instructions, skills):
        return RunnerState(
            "gateway.test", {"instructions": instructions, "skills": [s.id for s in skills]}
        )

    async def __call__(self, context):
        return RunResult(
            output=" ".join(str(item.payload) for item in context.inputs),
            wait_for=(),
            checkpoint=CheckpointWrite(
                context.checkpoint_number + 1,
                context.state,
                (),
                tuple(i.id for i in context.inputs),
            ),
        )


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[Any]:
    schema = "gw_test_" + uuid4().hex
    namespace = "gw_test:" + uuid4().hex
    settings = Settings(
        _env_file=None,
        database_schema=schema,
        valkey_namespace=namespace,
        control_token="admin-test",
        session_signing_key="signing-test",
        machine_tokens={"one": "machine-one", "two": "machine-two"},
        telegram_bot_token="12345:test-only",
        telegram_chat_id=-100,
    )
    await migrate(DATABASE, schema=schema)
    await migrate_state(DATABASE, schema=schema)
    try:
        async with AsyncConnectionPool(DATABASE, open=False) as pool:
            await pool.wait()
            metadata = Metadata(pool, schema=schema)
            skills = SkillService(pool, schema=schema)
            payloads = AgentPayloadStore(pool, schema=schema)
            await skills.initialize()
            await payloads.initialize()
            registry = MachineRegistry(Authenticator(settings), metadata, lambda: sessions)

            async def run(context):
                return await control.run(context)

            sessions = SessionService(
                database_url=DATABASE,
                valkey_url=VALKEY,
                runner=run,
                schema=schema,
                namespace=namespace,
            )
            control = ControlService(
                settings=settings,
                metadata=metadata,
                sessions=sessions,
                skills=skills,
                runner=cast(Any, EchoRunner()),
                machines=registry,
                payload_store=payloads,
            )
            async with sessions:
                yield control
            await registry.aclose()
    finally:
        async with await psycopg.AsyncConnection.connect(DATABASE) as conn:
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        async with Valkey.from_url(VALKEY) as valkey:
            keys = [key async for key in valkey.scan_iter(match=namespace + "*")]
            if keys:
                await valkey.delete(*keys)
