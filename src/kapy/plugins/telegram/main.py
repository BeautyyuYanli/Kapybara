"""One standalone Telegram process owns all its tasks, clients and database pools.

The CLI registry only calls main. Shutdown cancels and joins workers/runners before
closing clients; it does not submit a user cancellation or stop external services.
"""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from uuid import UUID

import httpx2
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from kapy.agent_runner import SessionBusy
from kapy.application.agent import create_agent
from kapy.application.resources import open_resources
from kapy.application.sessions import create_session_service
from kapy.control.models import ModelService

from .client import TelegramClient, TelegramFailure, retry_delay
from .controller import COMMANDS, TelegramController
from .delivery import TelegramDelivery
from .repository import TelegramRepository
from .schema import migrate
from .settings import StorageSettings, TelegramSettings
from .storage import open_storage

logger = logging.getLogger(__name__)


async def serve(settings: TelegramSettings) -> None:
    async with (
        open_resources(settings.common) as resources,
        open_storage(settings.database_path) as storage,
        httpx2.AsyncClient(timeout=settings.poll_timeout + 10, trust_env=False) as http,
    ):
        # Keep chat pacing when publisher batching is disabled.
        draft_interval = settings.common.output_flush_interval or 0.5
        client = TelegramClient(
            http,
            settings.bot_token.get_secret_value(),
            settings.api_base,
            draft_interval=draft_interval,
        )
        repository = TelegramRepository(async_sessionmaker(storage, expire_on_commit=False))
        sessions = create_session_service(resources, settings.common)
        agent = create_agent()
        failures = 0
        while True:
            try:
                bot = await client.api("getMe", {})
                await client.api(
                    "setMyCommands",
                    {
                        "commands": [
                            {"command": command, "description": description}
                            for command, description in COMMANDS.items()
                        ]
                    },
                )
                break
            except TelegramFailure as error:
                if error.code in {400, 401, 403, 409}:
                    raise
                await asyncio.sleep(retry_delay(error, failures))
                failures += 1
        runners: dict[UUID, asyncio.Task[None]] = {}
        accepting = True

        async def run_runner(session_id: UUID) -> None:
            try:
                await sessions.start_runner(
                    session_id,
                    agent=agent,
                    realtime_output=settings.common.realtime_output,
                    output_flush_interval=settings.common.output_flush_interval,
                )
            except SessionBusy:
                pass
            except Exception as error:
                logger.error("Runner failed for session %s: %s", session_id, type(error).__name__)
            finally:
                runners.pop(session_id, None)

        async with asyncio.TaskGroup() as tasks:

            def schedule_runner(session_id: UUID) -> None:
                if accepting and session_id not in runners:
                    runners[session_id] = tasks.create_task(run_runner(session_id))

            controller = TelegramController(
                client=client,
                sessions=sessions,
                models=ModelService(resources.core_session_factory),
                repository=repository,
                settings=settings,
                bot_id=bot["id"],
                username=bot["username"],
                schedule_runner=schedule_runner,
            )
            try:
                tasks.create_task(controller.poll())
                tasks.create_task(controller.process())
                tasks.create_task(
                    TelegramDelivery(
                        client,
                        sessions,
                        repository,
                        bot["id"],
                    ).run()
                )
                await asyncio.Future()
            finally:
                accepting = False


async def _serve_with_signals(settings: TelegramSettings) -> None:
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    assert task is not None
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await serve(settings)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kapy plugin telegram")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--database-path", type=Path)
    db_parser = commands.add_parser("db")
    db_parser.add_argument("operation", choices=["upgrade", "current", "revision"])
    db_parser.add_argument("--database-path", type=Path)
    db_parser.add_argument("--message")
    args = parser.parse_args(argv)
    try:
        values = {} if args.database_path is None else {"database_path": args.database_path}
        if args.command == "db":
            storage = StorageSettings(**values)
            asyncio.run(migrate(storage.database_path, args.operation, args.message))
        else:
            settings = TelegramSettings(**values)
            logging.basicConfig(level=settings.common.log_level)
            # HTTP request loggers may include the Bot API token in the request URL.
            for name in ("httpx", "httpx2", "httpcore"):
                logging.getLogger(name).setLevel(logging.WARNING)
            asyncio.run(_serve_with_signals(settings))
    except ValidationError:
        parser.error("Invalid Telegram configuration; see plugins/telegram/README.md")
    except KeyboardInterrupt, asyncio.CancelledError:
        return 0
    except Exception as error:
        logger.error("Telegram process failed: %s", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
