"""Short SQLite transactions only; no calls to the core service or Telegram API.

One process consumes inbox serially and one task owns each delivery row. Rows
returned here are detached snapshots; mutations explicitly save a new JSON value.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col, select

from .models import DefaultModelRow, DeliveryRow, InboxRow, PollRow, RouteRow

type DeliveryKey = tuple[int, int, int, UUID]


def delivery_key(row: DeliveryRow) -> DeliveryKey:
    return row.bot_id, row.chat_id, row.thread_id, row.session_id


class TelegramRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def default_model(self, bot_id: int) -> DefaultModelRow | None:
        async with self.sessions.begin() as db:
            return await db.get(DefaultModelRow, bot_id)

    async def set_default_model(self, bot_id: int, provider_id: UUID, model_name: str) -> None:
        """Replace the pair atomically; repeating a resolved inbox command is idempotent."""
        async with self.sessions.begin() as db:
            await db.execute(
                insert(DefaultModelRow)
                .values(bot_id=bot_id, provider_id=provider_id, model_name=model_name)
                .on_conflict_do_update(
                    index_elements=["bot_id"],
                    set_={"provider_id": provider_id, "model_name": model_name},
                )
            )

    async def offset(self, bot_id: int) -> int:
        async with self.sessions.begin() as db:
            row = await db.get(PollRow, bot_id)
            return row.next_update_id if row else 0

    async def ingest(self, bot_id: int, updates: list[dict[str, Any]]) -> None:
        """Persist the complete batch and offset atomically, before acknowledging via polling."""
        if not updates:
            return
        async with self.sessions.begin() as db:
            for payload in updates:
                message = payload.get("message") or {}
                row = InboxRow(
                    bot_id=bot_id,
                    update_id=payload["update_id"],
                    chat_id=message.get("chat", {}).get("id", 0),
                    thread_id=message.get("message_thread_id", 0),
                    payload=payload,
                )
                await db.execute(
                    insert(InboxRow).values(**row.model_dump()).on_conflict_do_nothing()
                )
            offset = max(item["update_id"] for item in updates) + 1
            statement = insert(PollRow).values(bot_id=bot_id, next_update_id=offset)
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=["bot_id"],
                    set_={"next_update_id": offset},
                    where=col(PollRow.next_update_id) < offset,
                )
            )

    async def next_inbox(self, bot_id: int) -> InboxRow | None:
        """Return the oldest unfinished update, preserving ordering through retries."""
        async with self.sessions.begin() as db:
            return (
                (
                    await db.execute(
                        select(InboxRow)
                        .where(col(InboxRow.bot_id) == bot_id, ~col(InboxRow.handled))
                        .order_by(col(InboxRow.update_id))
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )

    async def save_action(
        self,
        item: InboxRow,
        action: dict[str, Any],
        *,
        handled: bool = False,
        next_attempt_at: float = 0,
    ) -> None:
        async with self.sessions.begin() as db:
            await db.execute(
                update(InboxRow)
                .where(
                    col(InboxRow.bot_id) == item.bot_id,
                    col(InboxRow.update_id) == item.update_id,
                )
                .values(resolved_action=action, handled=handled, next_attempt_at=next_attempt_at)
            )
        item.resolved_action = action
        item.handled = handled
        item.next_attempt_at = next_attempt_at

    async def route(self, bot_id: int, chat_id: int, thread_id: int) -> UUID | None:
        async with self.sessions.begin() as db:
            row = await db.get(RouteRow, (bot_id, chat_id, thread_id))
            return row.session_id if row else None

    async def clear_route(
        self, bot_id: int, chat_id: int, thread_id: int, session_id: UUID
    ) -> None:
        async with self.sessions.begin() as db:
            await db.execute(
                delete(RouteRow).where(
                    col(RouteRow.bot_id) == bot_id,
                    col(RouteRow.chat_id) == chat_id,
                    col(RouteRow.thread_id) == thread_id,
                    col(RouteRow.session_id) == session_id,
                )
            )

    async def bind(self, item: InboxRow, action: dict[str, Any], session_id: UUID) -> None:
        """Record creation progress, route and a durable delivery in one private transaction."""
        action = action | {"session_id": str(session_id)}
        async with self.sessions.begin() as db:
            route = dict(bot_id=item.bot_id, chat_id=item.chat_id, thread_id=item.thread_id)
            await db.execute(
                insert(RouteRow)
                .values(**route, session_id=session_id)
                .on_conflict_do_update(index_elements=list(route), set_={"session_id": session_id})
            )
            delivery = DeliveryRow(
                **route,
                session_id=session_id,
                chat_type=item.payload["message"]["chat"].get("type", "group"),
            )
            await db.execute(
                insert(DeliveryRow).values(**delivery.model_dump()).on_conflict_do_nothing()
            )
            await db.execute(
                update(InboxRow)
                .where(
                    col(InboxRow.bot_id) == item.bot_id,
                    col(InboxRow.update_id) == item.update_id,
                )
                .values(resolved_action=action)
            )
        item.resolved_action = action

    async def deliveries(self, bot_id: int) -> tuple[DeliveryRow, ...]:
        async with self.sessions.begin() as db:
            return tuple(
                (
                    await db.execute(
                        select(DeliveryRow).where(
                            col(DeliveryRow.bot_id) == bot_id,
                        )
                    )
                ).scalars()
            )

    async def get_delivery(self, key: DeliveryKey) -> DeliveryRow:
        async with self.sessions.begin() as db:
            row = await db.get(DeliveryRow, key)
            if row is None:
                raise LookupError("Delivery does not exist")
            return row

    async def save_delivery(self, row: DeliveryRow) -> None:
        async with self.sessions.begin() as db:
            await db.merge(row)
