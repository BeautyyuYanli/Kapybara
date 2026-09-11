"""Bot protocol behavior through a real HTTP client with a local mock transport."""

import json

import httpx2
import pytest

from kapy.tmpv2.plugins.telegram.client import TelegramClient, TelegramFailure


@pytest.mark.asyncio
async def test_bot_api_uses_topic_and_rich_shape_without_leaking_errors():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(
            400,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: RICH_MESSAGE_TEXT_TOO_LONG",
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        client = TelegramClient(http, "private-token", "https://telegram.invalid")
        with pytest.raises(TelegramFailure) as raised:
            await client.send(123, 9, "hello", rich=True, draft_id=7)
    assert requests == [
        {
            "chat_id": 123,
            "message_thread_id": 9,
            "draft_id": 7,
            "rich_message": {"markdown": "hello"},
        }
    ]
    assert raised.value.rich_content_rejected
    assert "private-token" not in str(raised.value) and "RICH_MESSAGE" not in str(raised.value)


@pytest.mark.asyncio
async def test_transport_failure_is_sanitized_and_429_preserves_delay():
    def failed(request):
        raise httpx2.ConnectError("token-bearing upstream diagnostic", request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(failed)) as http:
        with pytest.raises(TelegramFailure) as raised:
            await TelegramClient(http, "secret", "https://telegram.invalid").api("getMe", {})
        assert raised.value.code == 503 and "diagnostic" not in str(raised.value)
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "parameters": {"retry_after": 17},
                },
            )
        )
    ) as http:
        with pytest.raises(TelegramFailure) as raised:
            await TelegramClient(http, "secret", "https://telegram.invalid").api("getMe", {})
        assert raised.value.code == 429 and raised.value.retry_after == 17
