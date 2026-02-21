"""Telegram Bot API client used by the long-poll starter."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

import anyio.to_thread as to_thread

_TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"


class TelegramBotApiError(RuntimeError):
    """Raised when Telegram Bot API returns a non-ok response or invalid JSON."""


@dataclass(slots=True)
class TelegramBotApi:
    """Minimal Telegram Bot API client for `getUpdates` polling."""

    token: str

    def _method_url(self, method: str) -> str:
        # Never log/print this URL; it embeds the bot token.
        return f"{_TELEGRAM_API_BASE}/bot{self.token}/{method}"

    def _send_message_sync(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id

        data = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(
            self._method_url("sendMessage"),
            data=data,
            method="POST",
        )
        request.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                raw = resp.read()
        except (
            urllib.error.HTTPError
        ) as e:  # pragma: no cover (hard to simulate reliably)
            raise TelegramBotApiError(
                f"Telegram sendMessage failed: HTTP {e.code}"
            ) from e
        except urllib.error.URLError as e:  # pragma: no cover (network dependent)
            raise TelegramBotApiError(
                "Telegram sendMessage failed: network error"
            ) from e

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise TelegramBotApiError(
                "Telegram sendMessage failed: invalid JSON"
            ) from e

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            desc = payload.get("description") if isinstance(payload, dict) else None
            raise TelegramBotApiError(
                "Telegram sendMessage failed"
                + (f": {desc}" if isinstance(desc, str) and desc else "")
            )

        result = payload.get("result")
        if not isinstance(result, dict):
            raise TelegramBotApiError(
                "Telegram sendMessage failed: missing result dict"
            )

        return result

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a message via `sendMessage` (async wrapper).

        Uses `parse_mode="HTML"` by default.
        """

        # Keep a minimal guard to avoid Telegram rejecting NUL-containing strings.
        safe_text = text.replace("\x00", "\ufffd")
        return await to_thread.run_sync(
            lambda: self._send_message_sync(
                chat_id=chat_id,
                text=safe_text,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
                parse_mode="HTML",
            )
        )

    def _get_me_sync(self) -> dict[str, Any]:
        request = urllib.request.Request(self._method_url("getMe"), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                raw = resp.read()
        except (
            urllib.error.HTTPError
        ) as e:  # pragma: no cover (hard to simulate reliably)
            raise TelegramBotApiError(f"Telegram getMe failed: HTTP {e.code}") from e
        except urllib.error.URLError as e:  # pragma: no cover (network dependent)
            raise TelegramBotApiError("Telegram getMe failed: network error") from e

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise TelegramBotApiError("Telegram getMe failed: invalid JSON") from e

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            desc = payload.get("description") if isinstance(payload, dict) else None
            raise TelegramBotApiError(
                "Telegram getMe failed"
                + (f": {desc}" if isinstance(desc, str) and desc else "")
            )

        result = payload.get("result")
        if not isinstance(result, dict):
            raise TelegramBotApiError("Telegram getMe failed: missing result dict")

        return result

    async def get_me(self) -> dict[str, Any]:
        """Fetch bot metadata via `getMe` (async wrapper)."""

        return await to_thread.run_sync(self._get_me_sync)

    def _get_updates_sync(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout_seconds,
            # Telegram `getUpdates` `limit` is capped (commonly 100). Use the
            # maximum to drain pending updates without needing a CLI knob.
            "limit": 10,
        }
        if offset is not None:
            params["offset"] = offset

        url = f"{self._method_url('getUpdates')}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, method="GET")

        # Client timeout should exceed server long-poll timeout.
        client_timeout = max(5, timeout_seconds + 15)
        try:
            with urllib.request.urlopen(request, timeout=client_timeout) as resp:
                raw = resp.read()
        except (
            urllib.error.HTTPError
        ) as e:  # pragma: no cover (hard to simulate reliably)
            raise TelegramBotApiError(
                f"Telegram getUpdates failed: HTTP {e.code}"
            ) from e
        except urllib.error.URLError as e:  # pragma: no cover (network dependent)
            raise TelegramBotApiError(
                "Telegram getUpdates failed: network error"
            ) from e

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise TelegramBotApiError("Telegram getUpdates failed: invalid JSON") from e

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            desc = payload.get("description") if isinstance(payload, dict) else None
            raise TelegramBotApiError(
                "Telegram getUpdates failed"
                + (f": {desc}" if isinstance(desc, str) and desc else "")
            )

        result = payload.get("result")
        if not isinstance(result, list):
            raise TelegramBotApiError("Telegram getUpdates failed: missing result list")

        updates: list[dict[str, Any]] = []
        for item in result:
            if isinstance(item, dict):
                updates.append(item)
        return updates

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        """Long-poll `getUpdates` (async wrapper).

        Note: stdlib `urllib` is blocking; this runs the request in a worker
        thread so the polling loop and agent tasks remain async-friendly.
        """

        return await to_thread.run_sync(
            lambda: self._get_updates_sync(
                offset=offset, timeout_seconds=timeout_seconds
            )
        )
