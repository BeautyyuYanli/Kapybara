"""Telegram owns its durable inbox, route and delivery schema."""

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

TABLES = (
    "gateway_telegram_poll (bot_id bigint PRIMARY KEY, next_update_id bigint NOT NULL)",
    "gateway_telegram_inbox (bot_id bigint NOT NULL, update_id bigint NOT NULL, "
    "chat_id bigint NOT NULL, thread_id bigint NOT NULL, payload jsonb NOT NULL, "
    "resolved_action jsonb, handled boolean NOT NULL DEFAULT false, "
    "next_attempt_at timestamptz, PRIMARY KEY(bot_id,update_id))",
    "gateway_telegram_routes (bot_id bigint NOT NULL, chat_id bigint NOT NULL, "
    "thread_id bigint NOT NULL, session_id uuid, config jsonb NOT NULL DEFAULT '{}', "
    "PRIMARY KEY(bot_id,chat_id,thread_id))",
    "gateway_telegram_delivery (bot_id bigint NOT NULL, chat_id bigint NOT NULL, "
    "thread_id bigint NOT NULL, session_id uuid NOT NULL, cursor text, "
    "projection jsonb NOT NULL DEFAULT '{}', item_offset integer NOT NULL DEFAULT 0, "
    "next_attempt_at timestamptz, blocked_error text, "
    "PRIMARY KEY(bot_id,chat_id,thread_id,session_id))",
)


async def migrate(pool: AsyncConnectionPool, *, schema: str) -> None:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        for definition in TABLES:
            await conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS " + definition))
