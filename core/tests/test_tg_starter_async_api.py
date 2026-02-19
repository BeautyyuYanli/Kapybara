import pytest

from k.starters.telegram import TelegramBotApi


@pytest.mark.anyio
async def test_telegram_bot_api_async_wrappers_call_sync_impl(monkeypatch) -> None:
    api = TelegramBotApi(token="test-token")

    got_me_called = False
    got_updates: tuple[int | None, int] | None = None
    got_send_message: tuple[int, str, int | None, str] | None = None

    def fake_get_me_sync(self):
        nonlocal got_me_called
        got_me_called = True
        return {"id": 123, "username": "MyBot"}

    def fake_get_updates_sync(self, *, offset: int | None, timeout_seconds: int):
        nonlocal got_updates
        got_updates = (offset, timeout_seconds)
        return [{"update_id": 1, "message": {"text": "hi"}}]

    def fake_send_message_sync(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        parse_mode: str = "HTML",
    ):
        nonlocal got_send_message
        got_send_message = (chat_id, text, reply_to_message_id, parse_mode)
        return {"message_id": 999}

    monkeypatch.setattr(TelegramBotApi, "_get_me_sync", fake_get_me_sync)
    monkeypatch.setattr(TelegramBotApi, "_get_updates_sync", fake_get_updates_sync)
    monkeypatch.setattr(TelegramBotApi, "_send_message_sync", fake_send_message_sync)

    me = await api.get_me()
    assert got_me_called is True
    assert me["id"] == 123

    updates = await api.get_updates(offset=5, timeout_seconds=12)
    assert got_updates == (5, 12)
    assert updates[0]["update_id"] == 1

    msg = await api.send_message(chat_id=42, text="hello", reply_to_message_id=7)
    assert got_send_message == (42, "hello", 7, "HTML")
    assert msg["message_id"] == 999
