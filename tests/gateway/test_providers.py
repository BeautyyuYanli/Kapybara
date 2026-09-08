"""Real storage transitions for shared providers, catalogs and session selections."""

import asyncio
import json
from uuid import UUID, uuid4

import httpx2
import pytest

from kapy.gateway.auth import Principal
from kapy.gateway.models import parse_session_model
from kapy.rpc import RpcError

from .conftest import MODEL_ID, PROVIDER_ID
from .test_control import OPERATOR, create
from .test_telegram import Bot, update

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def call(gateway, method, **params):
    if method in {
        "provider.create",
        "provider.update",
        "provider.delete",
        "provider.discover",
        "provider.model.create",
        "provider.model.update",
    }:
        params.setdefault("request_id", str(uuid4()))
    return await gateway.call(method, params, principal=OPERATOR)


async def test_provider_atomic_replay_key_rotation_cas_and_delete(gateway):
    request = str(uuid4())
    provider = await call(
        gateway, "provider.create", request_id=request, name="shared", api_key="very-private-key"
    )
    assert provider["type"] == "openai_responses" and provider["has_api_key"]
    assert "very-private-key" not in json.dumps(provider)
    assert provider == await call(
        gateway, "provider.create", request_id=request, name="shared", api_key="very-private-key"
    )
    with pytest.raises(RpcError) as conflict:
        await call(
            gateway, "provider.create", request_id=request, name="shared", api_key="changed-key"
        )
    assert conflict.value.code == -32009
    receipt = await gateway.metadata.request(UUID(request))
    assert "very-private-key" not in json.dumps(receipt, default=str)
    changed = await call(
        gateway,
        "provider.update",
        provider_id=provider["id"],
        expected_revision=1,
        name="renamed",
        type=provider["type"],
        base_url=provider["base_url"],
    )
    assert changed["revision"] == 2
    with pytest.raises(RpcError):
        await call(
            gateway,
            "provider.update",
            provider_id=provider["id"],
            expected_revision=2,
            name="unsafe",
            base_url="https://elsewhere.invalid/v1",
        )
    outcomes = await asyncio.gather(
        *[
            call(
                gateway,
                "provider.update",
                provider_id=provider["id"],
                expected_revision=2,
                name=str(i),
                type="openai_chat",
                base_url="https://elsewhere.invalid/v1",
                api_key=f"new-{i}",
            )
            for i in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(item, RpcError) for item in outcomes) == 1
    survivor = next(item for item in outcomes if isinstance(item, dict))
    await call(
        gateway,
        "provider.delete",
        provider_id=provider["id"],
        expected_revision=survivor["revision"],
    )
    row = (
        await gateway.metadata.rows(
            "SELECT api_key,deleted FROM gateway_providers WHERE id=%s", (UUID(provider["id"]),)
        )
    )[0]
    assert row == {"api_key": None, "deleted": True}
    with pytest.raises(RpcError):
        await call(gateway, "provider.get", provider_id=provider["id"])


async def test_catalog_discovery_persists_stable_ids_preserves_defaults_and_detects_endpoint_race(
    gateway,
):
    calls = []
    block = asyncio.Event()
    release = asyncio.Event()

    async def respond(request):
        calls.append(request)
        if len(calls) == 3:
            block.set()
            await release.wait()
        return httpx2.Response(
            200,
            json={
                "data": [
                    {"id": "test", "context_window_tokens": 500000, "max_output_tokens": 32000}
                ]
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        gateway.providers.http = http
        request = str(uuid4())
        first = await call(
            gateway, "provider.discover", provider_id=PROVIDER_ID, request_id=request
        )
        assert first == await call(
            gateway, "provider.discover", provider_id=PROVIDER_ID, request_id=request
        )
        assert len(calls) == 1
        item = first["items"][0]
        assert item["id"] == MODEL_ID
        updated = await call(
            gateway,
            "provider.model.update",
            model_id=MODEL_ID,
            expected_revision=item["revision"],
            defaults={"context_window_tokens": 100000, "max_output_tokens": 1000},
        )
        second = await call(gateway, "provider.discover", provider_id=PROVIDER_ID)
        assert second["items"][0]["id"] == MODEL_ID
        assert second["items"][0]["defaults"] == updated["defaults"]
        cached = await call(gateway, "provider.models", provider_id=PROVIDER_ID)
        assert len(calls) == 2 and cached["default_model_id"] == MODEL_ID
        pending = asyncio.create_task(call(gateway, "provider.discover", provider_id=PROVIDER_ID))
        await block.wait()
        await call(
            gateway,
            "provider.update",
            provider_id=PROVIDER_ID,
            expected_revision=1,
            name="changed",
            type="openai_responses",
            base_url="https://new.invalid/v1",
            api_key="new-key",
        )
        release.set()
        with pytest.raises(RpcError) as conflict:
            await pending
        assert conflict.value.code == -32009
        refreshed = await call(gateway, "provider.model.get", model_id=MODEL_ID)
        assert refreshed["discovered"] == {} and refreshed["discovered_at"] is None
        assert refreshed["defaults"] == updated["defaults"]


async def test_budget_precedence_live_defaults_and_session_selection_only(gateway):
    selection = parse_session_model({"model_id": MODEL_ID})
    _, _, window, output = await gateway.providers.effective(selection)
    assert (window, output) == (262144, 16384)
    await gateway.metadata.rows(
        "UPDATE gateway_provider_models SET discovered=%s WHERE id=%s",
        (json.dumps({"context_window_tokens": 200000, "max_output_tokens": 10000}), UUID(MODEL_ID)),
    )
    _, _, window, output = await gateway.providers.effective(selection)
    assert (window, output) == (200000, 10000)
    model = await call(gateway, "provider.model.get", model_id=MODEL_ID)
    await call(
        gateway,
        "provider.model.update",
        model_id=MODEL_ID,
        expected_revision=model["revision"],
        defaults={"context_window_tokens": 100000, "max_output_tokens": 2000},
    )
    created = await create(gateway)
    stored = created["session"]["config"]["model"]
    assert stored == {
        "model_id": MODEL_ID,
        "context_window_tokens": None,
        "max_output_tokens": None,
    }
    _, _, window, output = await gateway.providers.effective(parse_session_model(stored))
    assert (window, output) == (100000, 2000)
    _, _, window, output = await gateway.providers.effective(
        parse_session_model({**stored, "context_window_tokens": 90000, "max_output_tokens": 1000})
    )
    assert (window, output) == (90000, 1000)
    with pytest.raises(RpcError):
        await gateway.providers.effective(
            parse_session_model({**stored, "context_window_tokens": 900000})
        )
    await gateway.call(
        "session.delete",
        {"session_id": created["session"]["id"], "request_id": str(uuid4())},
        principal=OPERATOR,
    )
    assert (await call(gateway, "provider.get", provider_id=PROVIDER_ID))["has_api_key"]


async def test_session_provider_capability_scope_and_child_inheritance(gateway):
    parent_model = {
        "model_id": MODEL_ID,
        "context_window_tokens": 65536,
        "max_output_tokens": 1024,
    }
    parent = (await create(gateway, config={"model": parent_model}))["session"]["id"]
    identity = Principal("session", "one", UUID(parent))
    second = await call(gateway, "provider.create", name="other", api_key="other-key")
    outsider = await call(gateway, "provider.model.create", provider_id=second["id"], name="other")
    for method, params in [
        ("provider.list", {}),
        ("provider.discover", {"provider_id": PROVIDER_ID, "request_id": str(uuid4())}),
        ("provider.get", {"provider_id": second["id"]}),
    ]:
        with pytest.raises(RpcError):
            await gateway.call(method, params, principal=identity)
    visible = await gateway.call(
        "provider.models", {"provider_id": PROVIDER_ID}, principal=identity
    )
    assert visible["default_model_id"] == MODEL_ID
    child = await gateway.call(
        "session.create", {"request_id": str(uuid4()), "machine_ids": ["one"]}, principal=identity
    )
    assert child["session"]["config"]["model"] == parent_model
    alternative = await call(
        gateway,
        "provider.model.create",
        provider_id=PROVIDER_ID,
        name="another-model",
        defaults={"context_window_tokens": 90000, "max_output_tokens": 4096},
    )
    selection = {"model_id": alternative["id"], "max_output_tokens": 2048}
    switched = await create(gateway, identity, config={"model": selection})
    stored = switched["session"]["config"]["model"]
    assert stored == {**selection, "context_window_tokens": None}
    _, _, window, output = await gateway.providers.effective(parse_session_model(stored))
    assert (window, output) == (90000, 2048)
    with pytest.raises(RpcError):
        await create(gateway, identity, config={"model": {"model_id": outsider["id"]}})
    for forbidden in (
        {"type": "openai_chat"},
        {"api_key": "must-not-enter-history"},
        {"name": "raw-model"},
    ):
        with pytest.raises(RpcError):
            await create(gateway, config={"model": {"model_id": MODEL_ID, **forbidden}})
    requests = await gateway.metadata.rows("SELECT params,operation,result FROM gateway_requests")
    assert "must-not-enter-history" not in json.dumps(requests, default=str)


async def test_manual_models_cas_and_cache_pagination(gateway):
    extra = await call(
        gateway,
        "provider.model.create",
        provider_id=PROVIDER_ID,
        name="manual",
        defaults={"max_output_tokens": 1000},
    )
    with pytest.raises(RpcError):
        await call(gateway, "provider.model.create", provider_id=PROVIDER_ID, name="manual")
    page = await call(gateway, "provider.models", provider_id=PROVIDER_ID, limit=1)
    assert len(page["items"]) == 1 and page["default_model_id"] is None
    next_page = await call(
        gateway, "provider.models", provider_id=PROVIDER_ID, limit=1, after_id=page["next_after_id"]
    )
    assert {page["items"][0]["id"], next_page["items"][0]["id"]} == {MODEL_ID, extra["id"]}
    await call(
        gateway, "provider.model.update", model_id=extra["id"], expected_revision=1, defaults={}
    )
    with pytest.raises(RpcError):
        await call(
            gateway,
            "provider.model.update",
            model_id=extra["id"],
            expected_revision=1,
            defaults={"max_output_tokens": 500},
        )


async def test_google_discovery_filters_and_bounds_one_page(gateway):
    provider = await call(
        gateway, "provider.create", name="Gemini", type="google_ai_studio", api_key="google-key"
    )

    async def respond(request):
        assert request.headers["x-goog-api-key"] == "google-key"
        assert request.url.path == "/v1beta/models"
        return httpx2.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-test",
                        "supportedGenerationMethods": ["generateContent"],
                        "inputTokenLimit": 100000,
                        "outputTokenLimit": 5000,
                    },
                    {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
                ],
                "nextPageToken": "next",
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        gateway.providers.http = http
        found = await call(gateway, "provider.discover", provider_id=provider["id"])
        assert len(found["items"]) == 1 and found["items"][0]["name"] == "gemini-test"
        assert found["items"][0]["discovered"]["context_window_tokens"] == 100000
        assert found["next_page_token"] == "next"


async def test_history_prompt_query_executes_against_real_session_view(gateway):
    from kapy.agent.runner import BASE_INSTRUCTIONS

    made = await create(gateway, input="history marker")
    sid = made["session"]["id"]
    query = "SELECT seq,text FROM history WHERE seq>:n"
    assert query in BASE_INSTRUCTIONS and "data jsonb NOT NULL" in BASE_INSTRUCTIONS
    result = await gateway.call(
        "history.query", {"session_id": sid, "sql": query, "params": {"n": 0}}, principal=OPERATOR
    )
    assert result["rows"] and any("history marker" in str(row) for row in result["rows"])


async def test_telegram_provider_setup_is_private_and_not_model_input(gateway):
    from kapy.gateway.telegram import TelegramFrontend

    class FreshBot(Bot):
        route = TelegramFrontend.route

    bot = FreshBot(gateway)
    await bot.ingest([update(1, '/provider {"name":"New","api_key":"telegram-private-key"}')])
    await bot.process_once()
    route = await bot.route(-100, 0)
    assert route["config"]["provider_id"]
    assert "telegram-private-key" not in json.dumps(route, default=str)
    inbox = await gateway.metadata.rows(
        "SELECT payload,resolved_action FROM gateway_telegram_inbox"
    )
    assert "telegram-private-key" not in json.dumps(inbox)
    assert "telegram-private-key" not in json.dumps(bot.sent)
    assert not await gateway.metadata.rows("SELECT * FROM gateway_session_access")
    model = await call(
        gateway, "provider.model.create", provider_id=route["config"]["provider_id"], name="test"
    )
    await bot.ingest(
        [
            update(2, "/model " + model["id"]),
            update(3, "/settings"),
            update(4, "/machine one"),
            update(5, "hello"),
        ]
    )
    for _ in range(4):
        await bot.process_once()
    route = await bot.route(-100, 0)
    assert route["session_id"] is not None
    history = await gateway.sessions.read_history(route["session_id"])
    assert not any("api_key" in row.text or "/model" in row.text for row in history.items)


async def test_unpaged_openai_discovery_has_stable_bounded_pages(gateway):
    names = ["z-last", "a-first", "m-middle"]

    def respond(request):
        return httpx2.Response(200, json={"data": [{"id": name} for name in names]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        gateway.providers.http = http
        first = await call(gateway, "provider.discover", provider_id=PROVIDER_ID, limit=2)
        second = await call(
            gateway,
            "provider.discover",
            provider_id=PROVIDER_ID,
            limit=2,
            page_token=first["next_page_token"],
        )
    assert [item["name"] for item in first["items"]] == ["a-first", "m-middle"]
    assert [item["name"] for item in second["items"]] == ["z-last"]
    assert second["next_page_token"] is None


async def test_running_session_freezes_connection_and_next_run_uses_current_defaults(
    gateway, monkeypatch
):
    from agent.test_runner import Caller, response

    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def respond(request):
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            await release.wait()
            return response(name="process_list", call_id="frozen-call")
        return response(text="completed")

    async def completion(session_id, request_id):
        result = await gateway.call(
            "session.wait",
            {"session_id": session_id, "request_id": request_id, "wait_seconds": 10},
            principal=OPERATOR,
        )
        assert result["completion"] is not None
        return result["completion"]

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        gateway.http_client = http
        caller = Caller()
        monkeypatch.setattr(gateway.machines, "call", caller.call)
        made = await create(gateway, input="first")
        sid = made["session"]["id"]
        async with asyncio.timeout(10):
            await entered.wait()
        await call(
            gateway,
            "provider.update",
            provider_id=PROVIDER_ID,
            expected_revision=1,
            name="rotated",
            type="openai_chat",
            base_url="https://mock.invalid/v1",
            api_key="rotated-private-key",
        )
        await call(
            gateway,
            "provider.model.update",
            model_id=MODEL_ID,
            expected_revision=1,
            defaults={"context_window_tokens": 80000, "max_output_tokens": 2048},
        )
        release.set()
        first = await completion(sid, made["submission"]["request_id"])
        assert first["outcome"] == "completed"
        assert len(requests) == 2 and len(caller.calls) == 1
        assert caller.calls[0][1] == "process.list"
        assert any(
            message.get("tool_call_id") == "frozen-call"
            for message in json.loads(requests[1].content)["messages"]
        )
        request_id = str(uuid4())
        await gateway.call(
            "session.input",
            {"session_id": sid, "request_id": request_id, "payload": "second"},
            principal=OPERATOR,
        )
        second = await completion(sid, request_id)
        assert second["outcome"] == "completed"
        assert [r.headers["authorization"] for r in requests] == [
            "Bearer dummy",
            "Bearer dummy",
            "Bearer rotated-private-key",
        ]
        assert [json.loads(r.content)["max_completion_tokens"] for r in requests] == [
            16384,
            16384,
            2048,
        ]
        await call(gateway, "provider.delete", provider_id=PROVIDER_ID, expected_revision=2)
        request_id = str(uuid4())
        await gateway.call(
            "session.input",
            {"session_id": sid, "request_id": request_id, "payload": "third"},
            principal=OPERATOR,
        )
        failed = await completion(sid, request_id)
        assert failed["outcome"] == "failed" and len(requests) == 3
        assert "select another model" in failed["output"]
        history = await gateway.sessions.read_history(UUID(sid))
        assert "rotated-private-key" not in str(history)
        assert not http.is_closed


async def test_provider_command_terminal_delivery_failure_discards_private_ingress(gateway):
    from kapy.gateway.telegram import TelegramFailure

    class BlockedBot(Bot):
        async def send(self, chat, thread, text):
            raise TelegramFailure(403)

    bot = BlockedBot(gateway)
    await bot.ingest([update(1, '/provider {"name":"New","api_key":"secret-ingress"}')])
    await bot.process_once()
    inbox = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_inbox"))[0]
    assert inbox["handled"] and "secret-ingress" not in json.dumps(inbox, default=str)
    assert len(await gateway.metadata.rows("SELECT * FROM gateway_providers WHERE name='New'")) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 503])
async def test_discovery_error_is_acknowledged_and_does_not_block_topic_repairs(gateway, status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(status, json={"error": "api_key=private-discovery-response"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
        gateway.providers.http = http
        bot = Bot(gateway)
        await bot.ingest([update(1, "/discover"), update(2, "/providers")])
        await bot.process_once()
        first = (
            await gateway.metadata.rows("SELECT * FROM gateway_telegram_inbox WHERE update_id=1")
        )[0]
        assert first["resolved_action"]["kind"] == "reply" and not first["handled"]
        assert "private-discovery-response" not in json.dumps(first, default=str)
        # A lost Telegram acknowledgement can replay the safe reply, never discovery.
        bot.fail = True
        await bot.process_once()
        assert len(requests) == 1
        bot.fail = False
        await gateway.metadata.rows("UPDATE gateway_telegram_inbox SET next_attempt_at=NULL")
        await bot.process_once()
        await bot.process_once()
        rows = await gateway.metadata.rows(
            "SELECT handled FROM gateway_telegram_inbox ORDER BY update_id"
        )
        assert rows == [{"handled": True}, {"handled": True}]
        assert len(requests) == 1
        assert "private-discovery-response" not in json.dumps(bot.sent)
        assert any("use /discover to retry" in item[1].get("text", "") for item in bot.sent)


async def test_discovery_preserves_saved_selection_and_overrides(gateway):
    bot = Bot(gateway)
    selected = {"model_id": MODEL_ID, "context_window_tokens": 65536, "max_output_tokens": 1024}
    await bot.ingest([update(1, "/model " + json.dumps(selected))])
    await bot.process_once()
    before = await bot.route(-100, 0)

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={"data": [{"id": "test"}]})
        )
    ) as http:
        gateway.providers.http = http
        await bot.ingest([update(2, "/discover")])
        await bot.process_once()
    after = await bot.route(-100, 0)
    assert after["config"]["config"]["model"] == before["config"]["config"]["model"] == selected
    assert after["session_id"] == before["session_id"]


@pytest.mark.parametrize("known_configuration_failure", [True, False])
async def test_model_configuration_error_reaches_receipt_and_telegram_but_sdk_error_stays_private(
    gateway,
    known_configuration_failure,
):
    from pydantic_core import to_jsonable_python

    from kapy.gateway.telegram import project

    made = await create(gateway)
    sid = made["session"]["id"]
    if known_configuration_failure:
        await call(
            gateway,
            "provider.model.update",
            model_id=MODEL_ID,
            expected_revision=1,
            defaults={"context_window_tokens": 1000, "max_output_tokens": 2000},
        )
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                401,
                json={
                    "error": {
                        "message": "secret-provider-message https://private.invalid/?key=dummy"
                    }
                },
            )
        )
    ) as http:
        gateway.http_client = http
        request = str(uuid4())
        await gateway.call(
            "session.input",
            {"session_id": sid, "request_id": request, "payload": "run"},
            principal=OPERATOR,
        )
        result = await gateway.call(
            "session.wait",
            {"session_id": sid, "request_id": request, "wait_seconds": 5},
            principal=OPERATOR,
        )
    assert result["completion"]["outcome"] == "failed"
    records = to_jsonable_python((await gateway.sessions.read_output(UUID(sid))).items)
    _, rendered = project(records, {})
    text = rendered["pending"]["text"]
    if known_configuration_failure:
        assert "max_output_tokens must be smaller" in result["completion"]["output"]
        assert result["completion"]["output"] in text
    else:
        assert result["completion"]["output"] is None
        assert "secret-provider-message" not in str(records) + text
        assert "private.invalid" not in str(records) + text
