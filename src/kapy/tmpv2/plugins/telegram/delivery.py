"""Project SessionService.live into drafts and durable at-least-once messages.

Only a complete message advances after_seq. Persist one pending body before any
send, then acknowledge character offsets; a lost remote acknowledgement may replay
one chunk. Drafts sample the latest provisional state and never block live reads.
No database transaction spans sending, sleeping or generator iteration.
"""

import asyncio
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from uuid import uuid4

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from valkey.exceptions import ConnectionError as ValkeyConnectionError
from valkey.exceptions import TimeoutError as ValkeyTimeoutError

from kapy.tmpv2.agent_runner import MessageCommitted, TextDelta
from kapy.tmpv2.control.sessions import SessionService

from .client import TelegramClient, TelegramFailure, retry_delay, rich_chunk, text_chunk
from .models import DeliveryRow
from .repository import DeliveryKey, TelegramRepository, delivery_key

logger = logging.getLogger(__name__)


@dataclass
class Preview:
    """Transient response parts; discard the whole value after commit or reconnection."""

    seq: int = -1
    parts: dict[tuple[int, str], str] = field(default_factory=dict)
    progress: str = ""
    draft_id: int = field(default_factory=lambda: uuid4().int % (2**63 - 1) + 1)
    plain: bool = False
    unavailable: bool = False
    sent: tuple[str, bool] | None = None
    sent_at: float = 0

    def apply(self, event: TextDelta) -> None:
        if self.seq != event.response_seq:
            self.seq, self.parts, self.progress = event.response_seq, {}, ""
            self.draft_id = uuid4().int % (2**63 - 1) + 1
            self.plain, self.sent = False, None
        key = event.part_index, event.part_kind
        self.parts[key] = (
            event.text if event.op == "replace" else self.parts.get(key, "") + event.text
        )

    def content(self) -> tuple[str, bool]:
        text = "".join(value for (_, kind), value in sorted(self.parts.items()) if kind == "text")
        if text:
            return text, not self.plain
        thinking = "".join(
            value for (_, kind), value in sorted(self.parts.items()) if kind == "thinking"
        )
        return thinking[-2000:] or self.progress, False


class TelegramDelivery:
    def __init__(
        self,
        client: TelegramClient,
        sessions: SessionService,
        repository: TelegramRepository,
        bot_id: int,
        *,
        preview_interval: float = 0.5,
    ) -> None:
        self.client, self.sessions, self.repository, self.bot_id = (
            client,
            sessions,
            repository,
            bot_id,
        )
        self.preview_interval = preview_interval

    async def send_pending(self, row: DeliveryRow) -> None:
        """Attempt one persisted chunk; save retry/fallback/progress before returning."""
        pending = row.pending
        if pending is None or row.blocked_error:
            return
        if delay := max(0, row.next_attempt_at - time.time()):
            await asyncio.sleep(delay)
        remaining = pending["text"][row.item_offset :]
        rich = pending["format"] == "rich"
        chunk, remainder = rich_chunk(remaining) if rich else text_chunk(remaining)
        if rich and not chunk and remaining:
            row.pending = pending = pending | {"format": "plain"}
            await self.repository.save_delivery(row)
            rich = False
            chunk, remainder = text_chunk(remaining)
        try:
            if chunk:
                await self.client.send(row.chat_id, row.thread_id, chunk, rich=rich)
        except TelegramFailure as error:
            if error.code == 401:
                raise
            if rich and error.rich_content_rejected:
                row.pending = pending | {"format": "plain"}
            elif error.code in {400, 403}:
                row.blocked_error = str(error.code)
            else:
                row.next_attempt_at = time.time() + retry_delay(error)
            await self.repository.save_delivery(row)
            return
        row.next_attempt_at = 0
        if remainder:
            row.item_offset += len(chunk)
        else:
            row.after_seq = int(pending["seq"])
            row.pending, row.item_offset = None, 0
        await self.repository.save_delivery(row)

    async def send_preview(self, row: DeliveryRow, preview: Preview) -> None:
        if row.chat_type != "private" or preview.unavailable:
            return
        text, rich = preview.content()
        if not text:
            return
        chunk = rich_chunk(text)[0] if rich else text_chunk(text)[0]
        if rich and not chunk:
            preview.plain, rich = True, False
            chunk = text_chunk(text)[0]
        if preview.sent == (chunk, rich) and time.monotonic() - preview.sent_at < 20:
            return
        try:
            await self.client.send(
                row.chat_id,
                row.thread_id,
                chunk,
                rich=rich,
                draft_id=preview.draft_id,
            )
        except TelegramFailure as error:
            if error.code == 401:
                raise
            if rich and error.rich_content_rejected:
                preview.plain = True
            elif error.code in {400, 403}:
                preview.unavailable = True
            else:
                await asyncio.sleep(retry_delay(error))
            return
        preview.sent, preview.sent_at = (chunk, rich), time.monotonic()

    async def _refresh_preview(self, row: DeliveryRow, preview: Preview) -> None:
        """Sample at the publisher cadence, including send time; refresh unchanged drafts at 20s."""
        # Coalesce the first batch too, so a quick commit can replace the draft entirely.
        await asyncio.sleep(self.preview_interval)
        while not preview.unavailable:
            started = time.monotonic()
            await self.send_preview(row, preview)
            await asyncio.sleep(max(0, self.preview_interval - (time.monotonic() - started)))

    async def consume(self, row: DeliveryRow) -> None:
        """Own one live read and at most one draft task; commits cancel stale draft work."""
        while row.pending is not None and not row.blocked_error:
            await self.send_pending(row)
        if row.blocked_error:
            return
        preview = Preview()
        async with aclosing(self.sessions.live(row.session_id, after_seq=row.after_seq)) as events:
            pending_read = asyncio.create_task(anext(events))
            draft_task: asyncio.Task[None] | None = None
            try:
                while True:
                    waiting = {pending_read} if draft_task is None else {pending_read, draft_task}
                    done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                    if draft_task in done:
                        assert draft_task is not None
                        draft_task.result()
                        draft_task = None
                    if pending_read not in done:
                        continue
                    try:
                        event = pending_read.result()
                    except StopAsyncIteration:
                        return
                    if isinstance(event, TextDelta):
                        preview.apply(event)
                    elif isinstance(event, MessageCommitted):
                        # A draft may be waiting for the chat limiter or the network.
                        # Join its cancellation before queuing this committed response.
                        if draft_task is not None:
                            draft_task.cancel()
                            await asyncio.gather(draft_task, return_exceptions=True)
                            draft_task = None
                        preview = Preview(unavailable=preview.unavailable)
                        message = event.message.message
                        text = (
                            "".join(
                                part.content for part in message.parts if isinstance(part, TextPart)
                            )
                            if isinstance(message, ModelResponse)
                            else ""
                        )
                        if text:
                            row.pending = {"seq": event.message.seq, "text": text, "format": "rich"}
                            row.item_offset = 0
                            await self.repository.save_delivery(row)
                            while row.pending is not None and not row.blocked_error:
                                await self.send_pending(row)
                            if row.blocked_error:
                                return
                        else:
                            row.after_seq = event.message.seq
                            await self.repository.save_delivery(row)
                            for part in message.parts:
                                if isinstance(part, ToolCallPart):
                                    preview.progress = f"Calling tool: {part.tool_name}"
                                elif isinstance(part, ToolReturnPart):
                                    preview.progress = f"Tool finished: {part.tool_name}"
                    if (
                        draft_task is None
                        and row.chat_type == "private"
                        and not preview.unavailable
                        and preview.content()[0]
                    ):
                        draft_task = asyncio.create_task(self._refresh_preview(row, preview))
                    # Do not request another complete message while a send is pending.
                    pending_read = asyncio.create_task(anext(events))
            finally:
                pending_read.cancel()
                if draft_task is not None:
                    draft_task.cancel()
                await asyncio.gather(
                    pending_read,
                    *([draft_task] if draft_task is not None else []),
                    return_exceptions=True,
                )

    async def follow(self, key: DeliveryKey) -> None:
        failures = 0
        while True:
            try:
                row = await self.repository.get_delivery(key)
                await self.consume(row)
                return
            except SQLAlchemyError, ValkeyConnectionError, ValkeyTimeoutError, TimeoutError:
                logger.warning("Telegram live transport/storage unavailable; resuming from cursor")
                await asyncio.sleep(retry_delay(TelegramFailure(503), failures))
                failures += 1

    async def run(self) -> None:
        """Own every session follower, including deliveries from routes replaced by /new."""
        running: dict[DeliveryKey, asyncio.Task[None]] = {}
        failures = 0
        async with asyncio.TaskGroup() as tasks:
            while True:
                try:
                    rows = await self.repository.deliveries(self.bot_id)
                except OperationalError:
                    logger.warning(
                        "Telegram delivery discovery unavailable; existing followers continue"
                    )
                    await asyncio.sleep(retry_delay(TelegramFailure(503), failures))
                    failures += 1
                    continue
                failures = 0
                for row in rows:
                    key = delivery_key(row)
                    if row.blocked_error is None and (key not in running or running[key].done()):
                        running[key] = tasks.create_task(self.follow(key))
                await asyncio.sleep(1)
