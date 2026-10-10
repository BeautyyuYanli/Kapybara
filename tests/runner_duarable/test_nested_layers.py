"""Wrapped native combinations work across SDK serialization and real Activities."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from runner_duarable.nested_layer import Deps
from runner_duarable.nested_workflow import NestedWorkflow, agent, layers
from temporalio.worker import Replayer, Worker

from kapy.application.resources import connect_temporal
from kapy.control.sessions import CreateSession, SessionService
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.layers import load_layers
from kapy.layers.registry import LAYERS
from kapy.runner_duarable.activities import RunnerActivities


def deps(session_id):
    return Deps(
        session_id=session_id,
        payload=load_layers({"nested": {}, "other": {}}, {}, layers),
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_nested_children_return_outer_layer_state_before_hooks(runner_database, monkeypatch):
    for name, layer in layers.items():
        monkeypatch.setitem(LAYERS, name, layer)
    service = SessionService(runner_database.sessions)
    session = await service.create_session(
        CreateSession(provider_id=uuid4(), model_name="unused", layers={"nested": {}, "other": {}})
    )
    client = await connect_temporal(runner_database.settings)
    queue = f"nested-state-{uuid4()}"
    activities = RunnerActivities(runner_database.sessions, runner_database.outputs)
    async with Worker(
        client,
        task_queue=queue,
        workflows=[NestedWorkflow],
        activities=[activities.save_runner_state],
        plugins=[AgentPlugin(agent)],
    ):
        handle = await client.start_workflow(
            NestedWorkflow.run,
            deps(session.id),
            id=queue,
            task_queue=queue,
            execution_timeout=timedelta(seconds=30),
        )
        assert await handle.result() == "nested:1:A|hook:1;nested:2:B|hook:2;nested:3:C|hook:3"
        history = await handle.fetch_history()
    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, session.id)
        assert row is not None
        assert row.layer_states == {"nested": {"calls": 3, "hooks": 3}, "other": {"calls": 10}}
        assert row.runner_state_version == 1
        saved = await SessionRepository(db).read_runner_state(session.id)
    await Replayer(workflows=[NestedWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
        history
    )
    async with runner_database.sessions.begin() as db:
        repo = SessionRepository(db)
        assert await repo.read_runner_state(session.id) == saved
        row = await db.get(SessionRow, session.id)
        assert row is not None and row.layer_states == {
            "nested": {"calls": 3, "hooks": 3},
            "other": {"calls": 10},
        }
