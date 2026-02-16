"""CLI entrypoint for the Telegram starter."""

from __future__ import annotations

import argparse
import re
from typing import TYPE_CHECKING, Final

import logfire

if TYPE_CHECKING:
    from pydantic_ai.models import Model

from k.config import Config

from .compact import _expand_chat_id_watchlist
from .runner import _poll_and_run_forever
from .tz import _DEFAULT_TIMEZONE, _parse_timezone

_ID_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[,\s]+")


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="telegram",
        description="Telegram long-poll starter (getUpdates -> agent_run).",
    )
    parser.add_argument(
        "--model-name",
        required=True,
        help="PydanticAI model name passed to OpenRouterModel (required).",
    )
    parser.add_argument(
        "--token",
        required=True,
        help="Telegram bot token (never printed).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=60,
        help="Telegram getUpdates long-poll timeout seconds.",
    )
    parser.add_argument(
        "--keyword",
        required=True,
        help="Trigger substring. When at least one in-window update matches, send the whole in-window batch to the agent.",
    )
    parser.add_argument(
        "--chat_id",
        default="",
        help="Optional comma/space separated chat ids. When set, only those chats are processed.",
    )
    parser.add_argument(
        "--time-window-seconds",
        type=int,
        default=60,
        help="Only include updates within this age when date is available.",
    )
    parser.add_argument(
        "--timezone",
        default=_DEFAULT_TIMEZONE,
        help="Timezone for rendering compacted update `date` fields (default: UTC+8). "
        "Accepts IANA names (e.g. 'Asia/Shanghai') or offsets (e.g. 'UTC+8', '+08:00').",
    )
    return parser.parse_args(argv)


async def run(
    *,
    token: str,
    keyword: str,
    model: Model | str,
    chat_id: str = "",
    timezone: str = _DEFAULT_TIMEZONE,
    timeout_seconds: int = 60,
    time_window_seconds: int = 60,
) -> None:
    """Function entrypoint.

    `model` is required so callers must make an explicit model choice.
    """

    logfire.configure()
    logfire.instrument_pydantic_ai()

    config = Config()  # type: ignore[call-arg]

    try:
        tz = _parse_timezone(str(timezone))
    except ValueError as e:
        raise ValueError(f"Invalid timezone: {e}") from e

    chat_ids: set[int] | None
    raw_chat_ids = str(chat_id).strip()
    if not raw_chat_ids:
        chat_ids = None
    else:
        parts = [p for p in _ID_SPLIT_RE.split(raw_chat_ids) if p]
        try:
            chat_ids = {int(p) for p in parts}
        except ValueError as e:
            raise ValueError(f"Invalid chat_id entry in: {raw_chat_ids!r}") from e
        chat_ids = _expand_chat_id_watchlist(chat_ids)

    await _poll_and_run_forever(
        config=config,
        model=model,
        token=token,
        timeout_seconds=timeout_seconds,
        keyword=keyword,
        time_window_seconds=time_window_seconds,
        chat_ids=chat_ids,
        tz=tz,
    )


async def main() -> None:
    """CLI entrypoint."""
    args = _parse_cli_args()
    await run(
        token=args.token,
        keyword=args.keyword,
        model=args.model_name,
        chat_id=args.chat_id,
        timezone=args.timezone,
        timeout_seconds=args.timeout_seconds,
        time_window_seconds=args.time_window_seconds,
    )
