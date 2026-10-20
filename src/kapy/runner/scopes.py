"""Worker-owned business scopes; hooks never run under a database transaction.

Creation publishes all initial States together. Failed creation cleans every
configured layer with None, including partial/uncalled enters, while retaining
its current writer. Exit attempts save validated progress even on failure.
Creation uses the Config validated before insertion, without normalizing it twice.
External cleanup must be idempotent; ownership cannot revoke issued remote I/O.
"""

import asyncio
import logging
from copy import deepcopy
from uuid import UUID

import anyio
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from kapy.control.sessions.repository import SessionRepository
from kapy.control.sessions.types import OwnershipLost, SessionRecord
from kapy.layers.registry import layer_registry
from kapy.layers.scopes import retry_scope
from kapy.lifecycle import LifecycleStatus
from pydantic_ai_layer_protocol import dump_object
from pydantic_ai_layer_protocol.types import JsonObject

_logger = logging.getLogger(__name__)


async def check_owner(
    factory: async_sessionmaker[AsyncSession],
    session_id: UUID,
    activity_id: UUID,
    run_id: str,
    statuses: tuple[LifecycleStatus, ...],
    *,
    allow_cancelled: bool = False,
) -> None:
    if not allow_cancelled and activity.in_activity() and activity.is_cancelled():
        raise asyncio.CancelledError
    async with factory() as db, db.begin():
        await SessionRepository(db).guard(session_id, activity_id, run_id, statuses)


async def _exit_scope(
    factory: async_sessionmaker[AsyncSession],
    activity_id: UUID,
    run_id: str,
    session_id: UUID,
    name: str,
    config: BaseModel,
    saved_state: JsonObject | None,
    *,
    compensation: bool,
) -> None:
    """Revalidate and save State before another local exit attempt."""
    definition = layer_registry.definitions[name]
    exit_hook = definition.exit_scope
    if exit_hook is None:
        return
    state = (
        None if saved_state is None else definition.state_type.model_validate(deepcopy(saved_state))
    )

    async def attempt(exit_hook=exit_hook, config=config):
        await check_owner(
            factory,
            session_id,
            activity_id,
            run_id,
            (LifecycleStatus.CLOSING,),
            allow_cancelled=compensation,
        )
        await exit_hook(session_id, config.model_copy(deep=True), state)

    async def save_progress(name=name, state_type=definition.state_type):
        nonlocal state
        if state is None:
            return
        state = state_type.model_validate(dump_object(state))
        saved = dump_object(state)
        with anyio.CancelScope(shield=True):
            async with factory() as db, db.begin():
                session = await SessionRepository(db).guard(
                    session_id, activity_id, run_id, (LifecycleStatus.CLOSING,)
                )
                session.layer_states = session.layer_states | {name: saved}
                await db.flush()

    await retry_scope(attempt, after_attempt=save_progress)


async def _exit_scopes(
    factory: async_sessionmaker[AsyncSession],
    activity_id: UUID,
    run_id: str,
    session_id: UUID,
    configs: dict[str, BaseModel],
    states: dict[str, JsonObject],
    *,
    compensation: bool,
) -> bool:
    complete = True
    for name, config in reversed(configs.items()):
        try:
            await _exit_scope(
                factory,
                activity_id,
                run_id,
                session_id,
                name,
                config,
                states.get(name),
                compensation=compensation,
            )
        except OwnershipLost:
            raise
        except asyncio.CancelledError:
            if not compensation:
                raise
            complete = False
            _logger.exception(
                "Creation compensation cancelled for session %s layer %s", session_id, name
            )
        except Exception:
            complete = False
            _logger.exception("Scope cleanup failed for session %s layer %s", session_id, name)
    return complete


async def create_scopes(
    factory: async_sessionmaker[AsyncSession],
    activity_id: UUID,
    run_id: str,
    session_id: UUID,
    configs: dict[str, BaseModel],
) -> SessionRecord:
    states: dict[str, JsonObject] = {}
    try:
        for name, config in configs.items():
            definition = layer_registry.definitions[name]
            enter = definition.enter_scope
            if enter is None:
                states[name] = dump_object(definition.state_type.model_validate({}))
                continue

            async def attempt(enter=enter, config=config):
                await check_owner(
                    factory, session_id, activity_id, run_id, (LifecycleStatus.CREATING,)
                )
                return await enter(session_id, config.model_copy(deep=True))

            returned = await retry_scope(attempt)
            if not isinstance(returned, definition.state_type):
                raise TypeError(f"Layer {name!r} enter_scope must return its State")
            states[name] = dump_object(definition.state_type.model_validate(dump_object(returned)))
        await check_owner(factory, session_id, activity_id, run_id, (LifecycleStatus.CREATING,))
        async with factory() as db, db.begin():
            result = await SessionRepository(db).complete(
                session_id,
                activity_id,
                run_id,
                (LifecycleStatus.CREATING,),
                LifecycleStatus.READY,
                layer_states=states,
            )
        return result
    except BaseException:
        # A final commit acknowledgment can be lost. Never compensate until the
        # transaction ended and a conditional write confirms uncommitted creation.
        # A committed READY or a replacement writer prevents business compensation.
        with anyio.move_on_after(15, shield=True):
            async with factory() as db, db.begin():
                session = await SessionRepository(db).guard(
                    session_id, activity_id, run_id, (LifecycleStatus.CREATING,)
                )
                session.status = LifecycleStatus.CLOSING
                await db.flush()
            complete = await _exit_scopes(
                factory, activity_id, run_id, session_id, configs, {}, compensation=True
            )
            async with factory() as db, db.begin():
                await SessionRepository(db).complete(
                    session_id,
                    activity_id,
                    run_id,
                    (LifecycleStatus.CLOSING,),
                    LifecycleStatus.CLOSED if complete else LifecycleStatus.CLOSING,
                )
        raise


async def close_scopes(
    factory: async_sessionmaker[AsyncSession],
    activity_id: UUID,
    run_id: str,
    session_id: UUID,
    frozen: dict,
) -> SessionRecord:
    configs = layer_registry.prepare_configs(frozen["layers"])
    states = frozen["layer_states"]
    if states and states.keys() != configs.keys():
        raise ValueError("Saved layer states must match configured layers")
    complete = await _exit_scopes(
        factory, activity_id, run_id, session_id, configs, states, compensation=False
    )
    if not complete:
        raise RuntimeError("Session cleanup failed")
    await check_owner(factory, session_id, activity_id, run_id, (LifecycleStatus.CLOSING,))
    async with factory() as db, db.begin():
        result = await SessionRepository(db).complete(
            session_id,
            activity_id,
            run_id,
            (LifecycleStatus.CLOSING,),
            LifecycleStatus.CLOSED,
        )
    return result
