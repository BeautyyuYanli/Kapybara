"""Telegram Bot API client used by the long-poll starter.

All network/transport failures are normalized to :class:`TelegramBotApiError`
so callers can retry/back off without depending on HTTP client internals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

import httpx

_TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"


class TelegramBotApiError(RuntimeError):
    """Raised when Telegram Bot API calls fail for HTTP/network/payload reasons."""


@dataclass(slots=True)
class TelegramBotApi:
    """Minimal Telegram Bot API client for `getUpdates` polling."""

    token: str

    def _method_url(self, method: str) -> str:
        # Never log/print this URL; it embeds the bot token.
        return f"{_TELEGRAM_API_BASE}/bot{self.token}/{method}"

    async def _request_json(
        self,
        *,
        operation: str,
        method: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Execute one Telegram API request and return parsed JSON payload.

        Network/protocol failures are mapped to `TelegramBotApiError` so the
        polling loop can handle transient failures with one retry path.
        """

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.request(
                    method=method,
                    url=self._method_url(operation),
                    params=params,
                    data=data,
                )
            resp.raise_for_status()
        except (
            httpx.HTTPStatusError
        ) as e:  # pragma: no cover (hard to simulate reliably)
            raise TelegramBotApiError(
                f"Telegram {operation} failed: HTTP {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:  # pragma: no cover (network dependent)
            raise TelegramBotApiError(
                f"Telegram {operation} failed: network error"
            ) from e

        try:
            payload = resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            raise TelegramBotApiError(
                f"Telegram {operation} failed: invalid JSON"
            ) from e
        if not isinstance(payload, dict):
            raise TelegramBotApiError(f"Telegram {operation} failed: invalid JSON")
        return payload

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a message via `sendMessage`.

        Uses `parse_mode="HTML"` by default and normalizes transport failures
        to `TelegramBotApiError`.
        """

        parse_mode = "HTML"
        # Keep a minimal guard to avoid Telegram rejecting NUL-containing strings.
        safe_text = text.replace("\x00", "\ufffd")
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": safe_text,
            "parse_mode": parse_mode,
        }
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id

        payload = await self._request_json(
            operation="sendMessage",
            method="POST",
            data=params,
            timeout_seconds=10,
        )

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

    async def get_me(self) -> dict[str, Any]:
        """Fetch bot metadata via `getMe`."""

        payload = await self._request_json(
            operation="getMe",
            method="GET",
            timeout_seconds=10,
        )

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

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        """Long-poll `getUpdates`.

        Uses async `httpx` transport (no worker-thread shim required).
        """

        params: dict[str, Any] = {
            "timeout": timeout_seconds,
            # Telegram `getUpdates` `limit` is capped (commonly 100). Use the
            # maximum to drain pending updates without needing a CLI knob.
            "limit": 10,
        }
        if offset is not None:
            params["offset"] = offset

        # Client timeout should exceed server long-poll timeout.
        client_timeout = max(5, timeout_seconds + 15)
        payload = await self._request_json(
            operation="getUpdates",
            method="GET",
            params=params,
            timeout_seconds=client_timeout,
        )

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
