import httpx
import pytest
from kapy_collections.starters.telegram import TelegramBotApi, TelegramBotApiError
from kapy_collections.starters.telegram import api as telegram_api_module


@pytest.mark.anyio
async def test_telegram_bot_api_async_methods_call_request_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = TelegramBotApi(token="test-token")
    calls: list[
        tuple[str, str, dict[str, object] | None, dict[str, object] | None, float]
    ] = []

    async def fake_request_json(
        self: TelegramBotApi,
        *,
        operation: str,
        method: str,
        params: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        timeout_seconds: float,
    ) -> dict[str, object]:
        calls.append((operation, method, params, data, timeout_seconds))
        if operation == "getMe":
            return {"ok": True, "result": {"id": 123, "username": "MyBot"}}
        if operation == "getUpdates":
            return {"ok": True, "result": [{"update_id": 1, "message": {"text": "hi"}}]}
        if operation == "sendMessage":
            return {"ok": True, "result": {"message_id": 999}}
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(TelegramBotApi, "_request_json", fake_request_json)

    me = await api.get_me()
    assert me["id"] == 123

    updates = await api.get_updates(offset=5, timeout_seconds=12)
    assert updates[0]["update_id"] == 1

    msg = await api.send_message(chat_id=42, text="hello\x00", reply_to_message_id=7)
    assert msg["message_id"] == 999

    assert len(calls) == 3
    assert calls[0] == ("getMe", "GET", None, None, 10)
    assert calls[1] == (
        "getUpdates",
        "GET",
        {"timeout": 12, "limit": 10, "offset": 5},
        None,
        27,
    )
    assert calls[2] == (
        "sendMessage",
        "POST",
        None,
        {
            "chat_id": 42,
            "text": "hello\ufffd",
            "parse_mode": "HTML",
            "reply_to_message_id": 7,
        },
        10,
    )


@pytest.mark.anyio
async def test_request_json_wraps_remote_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = TelegramBotApi(token="test-token")

    async def fake_request(self: httpx.AsyncClient, *args: object, **kwargs: object):
        raise httpx.RemoteProtocolError("peer disconnected")

    monkeypatch.setattr(telegram_api_module.httpx.AsyncClient, "request", fake_request)

    with pytest.raises(
        TelegramBotApiError, match="Telegram getUpdates failed: network error"
    ) as exc_info:
        await api._request_json(
            operation="getUpdates",
            method="GET",
            timeout_seconds=5,
        )

    assert isinstance(exc_info.value.__cause__, httpx.RemoteProtocolError)


@pytest.mark.anyio
async def test_request_json_wraps_http_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = TelegramBotApi(token="test-token")

    async def fake_request(self: httpx.AsyncClient, *args: object, **kwargs: object):
        request = httpx.Request("GET", "https://api.telegram.org")
        return httpx.Response(status_code=503, request=request, content=b"{}")

    monkeypatch.setattr(telegram_api_module.httpx.AsyncClient, "request", fake_request)

    with pytest.raises(TelegramBotApiError, match="Telegram getMe failed: HTTP 503"):
        await api._request_json(
            operation="getMe",
            method="GET",
            timeout_seconds=5,
        )
