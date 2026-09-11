"""Telegram protocol and safe error categories, independent of the legacy gateway.

The caller owns the HTTP client. Exceptions never retain Bot API descriptions,
request objects or token-bearing URLs. Per-chat pacing also covers retries/drafts;
drafts use the publisher cadence while complete messages retain one-second spacing.
"""

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx2


@dataclass
class TelegramFailure(Exception):
    code: int
    retry_after: float = 1
    rich_content_rejected: bool = False


def retry_delay(error: TelegramFailure, attempt: int = 0) -> float:
    if error.code == 429:
        return max(1, error.retry_after)
    return min(30, 2 ** min(attempt, 5) + random.uniform(0, 1))


def text_chunk(text: str, units: int = 4000) -> tuple[str, str]:
    """Split at Unicode scalar boundaries while respecting Telegram UTF-16 limits."""
    count = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if count + width > units:
            boundary = text.rfind("\n\n", 0, index)
            if boundary >= index // 2:
                index = boundary + 2
            return text[:index], text[index:]
        count += width
    return text, ""


def rich_chunk(text: str) -> tuple[str, str]:
    """Conservative 32 KiB raw budget; split only on blank lines outside fences."""
    if len(text.encode("utf-8")) <= 32768:
        return text, ""
    position = size = boundary = 0
    fence = ""
    for line in text.splitlines(keepends=True):
        size += len(line.encode("utf-8"))
        if size > 32768:
            break
        position += len(line)
        content = line.rstrip("\r\n")
        marker = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", content)
        if marker:
            run, tail = marker.groups()
            if fence:
                if run[0] == fence[0] and len(run) >= len(fence) and not tail.strip():
                    fence = ""
            elif run[0] == "~" or "`" not in tail:
                fence = run
        elif not fence and not content.strip():
            boundary = position
    return text[:boundary], text[boundary:]


def rich_rejection(description: Any) -> bool:
    """Recognize explicit content failures only; never retain the API description."""
    if not isinstance(description, str):
        return False
    description = description.lower().removeprefix("bad request: ")
    # Architect's rejected-request samples: .context/delivery.md, commit ec34cc0.
    return description in {
        "rich_message_text_too_long",
        "rich_message_blocks_too_many",
        "rich_message_table_cols_too_many",
        "rich_message_depth_invalid",
    }


class TelegramClient:
    def __init__(
        self,
        client: httpx2.AsyncClient,
        token: str,
        api_base: str,
        *,
        draft_interval: float = 0.5,
    ) -> None:
        self.client = client
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.draft_interval = draft_interval
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._chat_ready: dict[int, float] = {}

    async def api(self, method: str, params: dict[str, Any]) -> Any:
        try:
            response = await self.client.post(
                f"{self.api_base}/bot{self.token}/{method}", json=params
            )
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Invalid response")
            if not response.is_success or body.get("ok") is not True:
                code = body.get("error_code", response.status_code)
                if not isinstance(code, int):
                    raise ValueError("Invalid status")
                rejected = (
                    method in {"sendRichMessage", "sendRichMessageDraft"}
                    and response.status_code == code == 400
                    and body.get("ok") is False
                    and rich_rejection(body.get("description"))
                )
                retry_after = float((body.get("parameters") or {}).get("retry_after", 1))
                raise TelegramFailure(code, retry_after, rejected)
            return body["result"]
        except httpx2.HTTPError, ValueError, TypeError, KeyError:
            raise TelegramFailure(503) from None

    async def send(
        self,
        chat: int,
        thread: int,
        text: str,
        *,
        rich: bool = False,
        draft_id: int | None = None,
    ) -> None:
        method = "sendRichMessage" if rich else "sendMessage"
        params: dict[str, Any] = {"chat_id": chat}
        params.update({"rich_message": {"markdown": text}} if rich else {"text": text})
        if draft_id is not None:
            method += "Draft"
            params["draft_id"] = draft_id
        if thread:
            params["message_thread_id"] = thread
        async with self._chat_locks.setdefault(chat, asyncio.Lock()):
            delay = self._chat_ready.get(chat, 0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            started = time.monotonic()
            interval = self.draft_interval if draft_id is not None else 1
            try:
                await self.api(method, params)
            except TelegramFailure as error:
                self._chat_ready[chat] = time.monotonic() + retry_delay(error)
                raise
            except asyncio.CancelledError:
                # A cancelled in-flight draft may already have reached Telegram.
                # Keep the pacing slot; cancelling while waiting above sends nothing.
                self._chat_ready[chat] = started + interval
                raise
            self._chat_ready[chat] = started + interval
