import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic_ai.models.function import AgentInfo, FunctionModel
from valkey.asyncio import Valkey

from kapy.agent import ModelFailure
from kapy.gateway import FrontendContext, Principal, create_app
from kapy.gateway.telegram import empty_projection
from kapy.rpc import RpcError
from kapy.settings import Settings

from .conftest import DATABASE, VALKEY, register_model
from .test_control import create
from .test_telegram import Bot, feed, install_output, record, sent_text, update


class Backend:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.tools: list[list[str]] = []

    def create_model(self, model_name: str) -> FunctionModel:
        self.names.append(model_name)

        async def stream(messages, info: AgentInfo) -> AsyncIterator[str]:
            self.tools.append([tool.name for tool in info.function_tools])
            yield "Backend reply"

        return FunctionModel(stream_function=stream)

    def classify_error(self, error: Exception) -> ModelFailure | None:
        return None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "configured, override, enabled",
    [(["apply_patch"], None, True), (["apply_patch"], (), False), ([], None, False)],
)
async def test_named_frontend_uses_only_control_port_and_borrowed_backend(
    monkeypatch, configured, override, enabled
):
    backend = Backend()
    ready = asyncio.get_running_loop().create_future()
    stopped = asyncio.Event()
    schema, namespace = "boundaries_" + uuid4().hex, "boundaries:" + uuid4().hex
    settings = Settings(
        database_url=DATABASE,
        valkey_url=VALKEY,
        database_schema=schema,
        valkey_namespace=namespace,
        control_token="admin",
        session_signing_key="signing",
        telegram_bot_token="leftover-token",
        telegram_chat_id=None,
        frontends=["terminal"],
        tool_plugins=configured,
    )

    class Terminal:
        def __init__(self, context: FrontendContext):
            self.control = context.control

        async def run(self) -> None:
            try:
                owner = Principal("frontend", frontend_id="terminal", subject="user:one")
                request = str(uuid4())
                config = await register_model(self.control, name="vendor/model-x")
                made = await self.control.call(
                    "session.create",
                    {
                        "request_id": request,
                        "input": "hello",
                        "config": config,
                    },
                    principal=owner,
                )
                assert isinstance(made, dict) and isinstance(made["session"], dict)
                sid = made["session"]["id"]
                assert isinstance(sid, str)
                await self.control.call(
                    "session.wait",
                    {"session_id": sid, "request_id": request, "wait_seconds": 5},
                    principal=owner,
                )
                output = await self.control.call(
                    "session.output", {"session_id": sid}, principal=owner
                )
                assert isinstance(output, dict) and output["items"]
                stranger = Principal("frontend", frontend_id="terminal", subject="user:two")
                with pytest.raises(RpcError):
                    await self.control.call("session.get", {"session_id": sid}, principal=stranger)
                next_request = str(uuid4())
                await self.control.call(
                    "session.input",
                    {"session_id": sid, "request_id": next_request, "payload": "again"},
                    principal=owner,
                )
                status = await self.control.call(
                    "session.wait",
                    {"session_id": sid, "request_id": next_request, "wait_seconds": 5},
                    principal=owner,
                )
                assert isinstance(status, dict) and isinstance(status["completion"], dict)
                assert status["completion"]["output"] == "Backend reply"
                await self.control.call(
                    "session.delete",
                    {"session_id": sid, "request_id": str(uuid4())},
                    principal=owner,
                )
                with pytest.raises(RpcError):
                    await self.control.call("session.get", {"session_id": sid}, principal=owner)
                ready.set_result(None)
                await asyncio.Event().wait()
            except Exception as error:
                if not ready.done():
                    ready.set_exception(error)
                raise
            finally:
                stopped.set()

    app = create_app(
        settings,
        model_backend_factory=lambda connection, http: backend,
        frontend_factories={"terminal": Terminal},
        plugins=override,
    )
    try:
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(ready, 10)
            tables = await app.state.metadata.rows(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (schema,)
            )
            assert not any(row["table_name"].startswith("gateway_telegram") for row in tables)
        assert stopped.is_set()
        assert backend.names == ["vendor/model-x", "vendor/model-x"]
        assert all(("apply_patch" in names) == enabled for names in backend.tools)
    finally:
        async with await psycopg.AsyncConnection.connect(DATABASE) as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
        async with Valkey.from_url(VALKEY) as valkey:
            keys = [key async for key in valkey.scan_iter(match=namespace + "*")]
            if keys:
                await valkey.delete(*keys)


def test_unknown_plugins_rejected_before_resources_and_explicit_disable():
    with pytest.raises(ValueError, match="Unknown frontends"):
        create_app(Settings(frontends=["missing"]))
    with pytest.raises(ValueError, match="Unknown or repeated tool"):
        create_app(Settings(frontends=[], tool_plugins=["missing"]))
    create_app(
        Settings(frontends=[], telegram_bot_token="unused", telegram_chat_id=None), plugins=()
    )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("identity", ["terminal:user:one", "telegram:12345:-100:7"])
async def test_generic_frontend_create_intent_recovers_and_keeps_existing_owner(
    gateway, monkeypatch, identity
):
    principal = Principal.from_id(identity)
    assert principal.id == identity
    original = gateway.metadata.finish

    async def crash(*args, **kwargs):
        raise OSError("commit interrupted")

    monkeypatch.setattr(gateway.metadata, "finish", crash)
    request_id = str(uuid4())
    with pytest.raises(OSError):
        await create(gateway, principal, request_id=request_id, input="recover generic owner")
    monkeypatch.setattr(gateway.metadata, "finish", original)
    await gateway.recover()
    listed = await gateway.call("session.list", {}, principal=principal)
    assert len(listed["items"]) == 1
    sid = listed["items"][0]["id"]
    status = await gateway.call(
        "session.wait",
        {"session_id": sid, "request_id": request_id, "wait_seconds": 5},
        principal=principal,
    )
    assert status["completion"]["output"] == "recover generic owner"
    assert (await gateway.metadata.access(sid))["owner_id"] == identity


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("blocked", [False, True])
async def test_telegram_cleans_deleted_pending_even_when_blocked(gateway, monkeypatch, blocked):
    records: list[dict[str, Any]] = []
    bot, sid = await install_output(gateway, monkeypatch, records)
    feed(records, record("final", "do not send"))
    bot.fail = True

    # Persist a pending projection without delivering a successful message.
    async def reject(*args):
        from kapy.gateway.telegram import TelegramFailure

        raise TelegramFailure(429, 10)

    monkeypatch.setattr(bot, "send_rich", reject)
    await bot.deliver_once()
    rows = await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery")
    assert rows[0]["projection"]["pending"]["text"] == "do not send"
    await gateway.metadata.rows(
        "UPDATE gateway_telegram_delivery SET blocked_error=%s", ("403" if blocked else None,)
    )
    await gateway.call(
        "session.delete",
        {"session_id": str(sid), "request_id": str(uuid4())},
        principal=Principal("operator"),
    )
    # Core deletion intentionally leaves adapter state alone; a restored adapter cleans it.
    assert await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery")
    restored = Bot(gateway)
    await restored.deliver_once()
    assert restored.sent == []
    assert await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery") == []
    assert (await restored.route(-100, 0))["session_id"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_telegram_legacy_origin_uses_saved_receipt_without_creating_sessions(gateway):
    bot = Bot(gateway)
    await bot.ingest([update(1, "/machine one"), update(2, "/new"), update(3, "/new")])
    for _ in range(3):
        await bot.process_once()
    before = await gateway.metadata.rows(
        "SELECT session_id FROM gateway_session_access ORDER BY session_id"
    )
    await gateway.metadata.rows(
        "UPDATE gateway_telegram_inbox SET resolved_action=resolved_action-'session_id'"
    )
    await bot.restore_origins()
    rows = await gateway.metadata.rows(
        "SELECT resolved_action FROM gateway_telegram_inbox "
        "WHERE resolved_action->>'kind'='create' ORDER BY update_id"
    )
    assert len(rows) == 2 and len({row["resolved_action"]["session_id"] for row in rows}) == 2
    assert (
        await gateway.metadata.rows(
            "SELECT session_id FROM gateway_session_access ORDER BY session_id"
        )
        == before
    )

    # A later pass has nothing to replay.
    async def no_call(*args, **kwargs):
        raise AssertionError("already restored")

    bot.control = type("Port", (), {"call": staticmethod(no_call)})()
    await bot.restore_origins()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_legacy_origin_waits_for_original_inbox_retry_before_delivery(gateway):
    bot = Bot(gateway)
    await bot.ingest([update(1, "/machine one"), update(2, "/new"), update(3, "/new")])
    await bot.process_once()
    await bot.process_once()
    bot.fail = True
    await bot.process_once()
    inboxes = await gateway.metadata.rows(
        "SELECT * FROM gateway_telegram_inbox "
        "WHERE resolved_action->>'kind'='create' ORDER BY update_id"
    )
    assert len(inboxes) == 2
    assert inboxes[0]["handled"] is True
    assert inboxes[1]["handled"] is False and inboxes[1]["next_attempt_at"] is not None
    session_ids = [row["resolved_action"]["session_id"] for row in inboxes]
    params = [row["resolved_action"]["params"] for row in inboxes]
    # The old implementation saved neither origin, including the committed create
    # whose confirmation hit Telegram backoff after its delivery row was saved.
    await gateway.metadata.rows(
        "UPDATE gateway_telegram_inbox SET resolved_action=resolved_action-'session_id' "
        "WHERE resolved_action->>'kind'='create'"
    )
    for sid, text in zip(
        session_ids, ("older session output", "later session output"), strict=True
    ):
        projection = empty_projection()
        projection["pending"] = {"text": text, "next": empty_projection()}
        await gateway.metadata.rows(
            "UPDATE gateway_telegram_delivery SET projection=%s WHERE session_id=%s",
            (Jsonb(projection), sid),
        )
    restored = Bot(gateway)
    await restored.deliver_once()
    assert restored.sent == []
    deferred = (
        await gateway.metadata.rows("SELECT * FROM gateway_telegram_inbox WHERE update_id=3")
    )[0]
    assert deferred["handled"] is False
    assert "session_id" not in deferred["resolved_action"]
    assert deferred["next_attempt_at"] == inboxes[1]["next_attempt_at"]

    await gateway.metadata.rows(
        "UPDATE gateway_telegram_inbox SET next_attempt_at=NULL WHERE update_id=3"
    )
    await restored.process_once()
    completed = await gateway.metadata.rows(
        "SELECT * FROM gateway_telegram_inbox "
        "WHERE resolved_action->>'kind'='create' ORDER BY update_id"
    )
    assert all(row["handled"] for row in completed)
    assert [row["resolved_action"]["session_id"] for row in completed] == session_ids
    assert [row["resolved_action"]["params"] for row in completed] == params
    assert len((await gateway.sessions.list_sessions()).items) == 2
    restored.sent.clear()
    await restored.deliver_once()
    await restored.deliver_once()
    assert [sent_text(payload) for _, payload in restored.sent] == [
        "older session output",
        "later session output",
    ]
