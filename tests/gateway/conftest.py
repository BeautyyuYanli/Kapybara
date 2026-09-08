import os
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
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
from kapy.gateway.telegram_storage import migrate as migrate_telegram
from kapy.rpc import JsonObject
from kapy.settings import Settings
from kapy.skills import SkillService
from kapy.state import CheckpointWrite, RunnerState, RunResult, SessionService
from kapy.state import migrate as migrate_state

MODEL_ID = "667bb0e0-0843-4690-a081-d39d0510a553"
PROVIDER_ID = "17f346de-8076-4eb5-9c54-2b9610377152"
MODEL_CONFIG: JsonObject = {"model": {"model_id": MODEL_ID}}

DATABASE = os.environ.get("KAPY_DATABASE_URL", "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy")
VALKEY = os.environ.get("KAPY_VALKEY_URL", "redis://127.0.0.1:56379/0")


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
        async with AsyncConnectionPool(DATABASE, open=False) as pool, httpx2.AsyncClient() as http:
            await pool.wait()
            await migrate_telegram(pool, schema=schema)
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
                http_client=http,
                machines=registry,
                payload_store=payloads,
            )
            await metadata.rows(
                "INSERT INTO gateway_providers(id,name,type,base_url,api_key) VALUES (%s,'test','openai_chat','https://mock.invalid/v1','dummy')",
                (UUID(PROVIDER_ID),),
            )
            await metadata.rows(
                "INSERT INTO gateway_provider_models(id,provider_id,name) VALUES (%s,%s,'test')",
                (UUID(MODEL_ID), UUID(PROVIDER_ID)),
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


async def register_model(
    control, *, name="test", base_url="https://mock.invalid/v1", protocol="openai_chat"
) -> JsonObject:
    from kapy.gateway.auth import Principal

    principal = Principal("operator")
    provider = await control.call(
        "provider.create",
        {
            "request_id": str(uuid4()),
            "name": "test",
            "type": protocol,
            "base_url": base_url,
            "api_key": "dummy",
        },
        principal=principal,
    )
    model = await control.call(
        "provider.model.create",
        {"request_id": str(uuid4()), "provider_id": provider["id"], "name": name},
        principal=principal,
    )
    return {"model": {"model_id": model["id"]}}
