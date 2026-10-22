"""Real PostgreSQL migration histories preserve data and isolate table ownership."""

import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from psycopg import sql
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateSchema

from kapy.agent_history.models import history_metadata
from kapy.application.resources import open_core_database
from kapy.application.settings import CommonSettings
from kapy.control.database import ControlTable
from kapy.database.migration import migration_config
from kapy.database.schema import OWNED_TABLES, migrate
from kapy.layers.shell.models import Config as ShellConfig

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("branch_revision", ["da2b698fbbac", "4a86521aa2ff", "5de14eebe20b"])
async def test_durable_only_closes_legacy_sessions_and_retains_history_snapshots_and_leases(
    database,
    branch_revision,
):
    schema = "durable_only_migration_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    checkpoint_id, plugin_id, durable_id, empty_id = [uuid4() for _ in range(4)]
    messages: list[ModelMessage] = [
        ModelRequest(
            [UserPromptPart("Continue reading the retained shell job.")],
            metadata={"seq": 3, "scope": {"turn": 7}},
        ),
        ModelResponse(
            [TextPart("The retained job has produced output through byte 1024.")],
            metadata={"seq": 4},
            provider_details={"request_id": "retained-model-response"},
        ),
    ]
    snapshot = ModelMessagesTypeAdapter.dump_json(messages).decode()
    layers = {
        "shell": ShellConfig(
            cwd="/workspace/retained",
            base_url="http://shellctl:8765/",
            env={"LANG": "C.UTF-8", "TASK": "retained-migration-job"},
            token_env="MIGRATION_SHELL_TOKEN",
            redact_patterns=[r"secret-\d+"],
        ).model_dump(mode="json")
    }
    # State at da2b698fbbac predates the required shell scope identity.
    layer_states = {"shell": {"jobs": {"retained-job-a": 1024, "retained-job-b": 57}}}
    try:
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(CreateSchema(schema))

                def config(connection):
                    connection.dialect.default_schema_name = schema
                    return migration_config(
                        connection,
                        directory=Path(__file__).parents[2] / "src/kapy/database/migrations",
                        metadata=[ControlTable.metadata, history_metadata],
                        version_table="core_schema_version",
                        owns_table=OWNED_TABLES.__contains__,
                    )

                await db.run_sync(lambda conn: command.upgrade(config(conn), "da2b698fbbac"))
                for session_id in (checkpoint_id, plugin_id, durable_id, empty_id):
                    await db.execute(
                        text("""INSERT INTO sessions
                            (id, provider_id, model_name, title, status, model_settings,
                             compaction_replay_turns, runner_state, runner_state_version,
                             layers, layer_states, created_at, updated_at)
                            VALUES (:id, :provider, 'test', 'retained', 'ready', '{}', 10,
                                    :state, :version, CAST(:layers AS JSON),
                                    CAST(:layer_states AS JSON),
                                    '2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z')"""),
                        {
                            "id": session_id,
                            "provider": uuid4(),
                            "state": snapshot if session_id == durable_id else None,
                            "version": 7 if session_id == durable_id else 0,
                            "layers": json.dumps(layers if session_id == durable_id else {}),
                            "layer_states": json.dumps(
                                layer_states if session_id == durable_id else {}
                            ),
                        },
                    )
                    history: list[ModelMessage] = (
                        messages if session_id == durable_id else [ModelRequest([])]
                    )
                    payloads = ModelMessagesTypeAdapter.dump_python(history, mode="json")
                    for seq, payload in enumerate(payloads, start=3):
                        kind, parts = payload.pop("kind"), payload.pop("parts")
                        payload.pop("usage", None)
                        payload.pop("finish_reason", None)
                        await db.execute(
                            text("""INSERT INTO agent_history
                                (session_id, seq, kind, message, message_metadata, created_at)
                                VALUES (:id, :seq, :kind, CAST(:message AS JSON),
                                        CAST(:metadata AS JSON), '2026-09-01T00:00:00Z')"""),
                            {
                                "id": session_id,
                                "seq": seq,
                                "kind": kind,
                                "message": json.dumps({"parts": parts}),
                                "metadata": json.dumps(payload),
                            },
                        )
                await db.execute(
                    text("INSERT INTO agent_states (session_id, next_step) VALUES (:id, 'done')"),
                    {"id": checkpoint_id},
                )
                await db.execute(
                    text("""INSERT INTO agent_context_pages
                        (session_id, anchor_seq, policy_key, payload)
                        VALUES (:id, 3, 'summary/v1', '{"summary":"legacy"}')"""),
                    {"id": checkpoint_id},
                )
                await db.execute(
                    text("""INSERT INTO plugin_agent_bindings
                        (session_id, plugin_provider, plugin_name, data_version, config, state,
                         revision, status, created_at, updated_at)
                        VALUES (:id, 'builtin', 'shell', 1, '{}', '{"jobs":{}}', :revision,
                                'ready', now(), now())"""),
                    {"id": plugin_id, "revision": uuid4()},
                )
                await db.execute(
                    text("""INSERT INTO session_leases (session_id, lock_token, heartbeat_at)
                        VALUES (:id, :token, '2026-09-01T00:00:00Z')"""),
                    {"id": durable_id, "token": uuid4()},
                )
                before = (
                    await db.execute(text("SELECT * FROM agent_history ORDER BY session_id, seq"))
                ).all()
                leases = (await db.execute(text("SELECT * FROM session_leases"))).all()
                await db.run_sync(lambda conn: command.upgrade(config(conn), branch_revision))
                await db.run_sync(lambda conn: command.upgrade(config(conn), "5be81e905907"))
                await db.run_sync(lambda conn: command.upgrade(config(conn), "5be81e905907"))
                assert (
                    await db.execute(text("SELECT * FROM agent_history ORDER BY session_id, seq"))
                ).all() == before
                assert (await db.execute(text("SELECT * FROM session_leases"))).all() == leases
                rows = {r.id: r for r in (await db.execute(text("SELECT * FROM sessions"))).all()}
                for session_id in (checkpoint_id, plugin_id):
                    assert rows[session_id].status == "closed"
                    assert rows[session_id].runner_state is None
                    assert rows[session_id].runner_state_version == 0
                for session_id in (durable_id, empty_id):
                    assert rows[session_id].status == "ready"
                    assert rows[session_id].updated_at.isoformat() == "2026-09-02T00:00:00+00:00"
                assert (rows[durable_id].runner_state, rows[durable_id].runner_state_version) == (
                    snapshot,
                    7,
                )
                assert rows[durable_id].layers == layers
                assert rows[durable_id].layer_states == {
                    "shell": {"scope_id": str(durable_id), **layer_states["shell"]}
                }
                assert all(r.title == "retained" for r in rows.values())
                for session_id in (checkpoint_id, plugin_id, empty_id):
                    assert rows[session_id].layers == {}
                    assert rows[session_id].layer_states == {}
                assert not set(await db.run_sync(lambda conn: inspect(conn).get_table_names())) & {
                    "agent_states",
                    "agent_context_pages",
                    "plugin_agent_bindings",
                    "session_inputs",
                    "session_cancels",
                }
                assert not {
                    c["name"]
                    for c in await db.run_sync(lambda conn: inspect(conn).get_columns("sessions"))
                } & {"context_plugin", "compaction_threshold_tokens", "compaction_replay_turns"}
                assert not await db.run_sync(
                    lambda conn: inspect(conn).get_check_constraints("sessions")
                )
                with pytest.raises(NotImplementedError, match="irreversible"):
                    await db.run_sync(lambda conn: command.downgrade(config(conn), "da2b698fbbac"))
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as db:
            await db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
async def test_temporal_ownership_upgrade_drops_ledger_preserves_business_and_pairs_ids(database):
    schema = "temporal_ownership_migration_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    statuses = ("creating", "ready", "starting", "running", "closing", "closed")
    try:
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(CreateSchema(schema))

                def config(connection):
                    connection.dialect.default_schema_name = schema
                    return migration_config(
                        connection,
                        directory=Path(__file__).parents[2] / "src/kapy/database/migrations",
                        metadata=[ControlTable.metadata, history_metadata],
                        version_table="core_schema_version",
                        owns_table=OWNED_TABLES.__contains__,
                    )

                await db.run_sync(lambda conn: command.upgrade(config(conn), "33472a5802a2"))
                for status in statuses:
                    identity, activity_id = uuid4(), uuid4()
                    await db.execute(
                        text("""INSERT INTO sessions
                        (id, status, title, provider_id, model_name, model_settings,
                         layers, layer_states, runner_state, current_activity_id,
                         created_at, updated_at)
                        VALUES (:id, :status, :title, :provider, 'test', '{"temperature": 0.5}',
                            '{"scope":{"config":"retained"}}', '{"scope":{"progress": 7}}',
                            'opaque snapshot', :activity, now(), now())"""),
                        {
                            "id": identity,
                            "status": status,
                            "title": status,
                            "provider": uuid4(),
                            "activity": activity_id,
                        },
                    )
                    await db.execute(
                        text("""INSERT INTO session_operations
                        (activity_id, session_id, kind, phase, request, input, created_at, deadline)
                        VALUES (:activity, :id, 'run', 'running', '{}',
                            '{"api_key":"private-copy"}', now(), now())"""),
                        {"id": identity, "activity": activity_id},
                    )
                    await db.execute(
                        text("""INSERT INTO agent_history
                        (session_id,seq,kind,message,message_metadata,created_at)
                        VALUES (:id,3,'request','{"parts":[]}','{}',now())"""),
                        {"id": identity},
                    )
                before = (
                    await db.execute(text("SELECT * FROM agent_history ORDER BY session_id"))
                ).all()
                await db.run_sync(lambda conn: command.upgrade(config(conn), "head"))
                rows = (await db.execute(text("SELECT * FROM sessions"))).all()
                assert {row.title: row.status for row in rows} == {
                    status: status if status in {"ready", "closed"} else "closing"
                    for status in statuses
                }
                for row in rows:
                    assert row.layers == {"scope": {"config": "retained"}}
                    assert row.layer_states == {"scope": {"progress": 7}}
                    assert row.runner_state == "opaque snapshot"
                    assert row.model_settings == {"temperature": 0.5}
                    assert row.current_activity_id is row.current_activity_run_id is None
                assert "session_operations" not in await db.run_sync(
                    lambda conn: inspect(conn).get_table_names()
                )
                assert (
                    await db.execute(text("SELECT * FROM agent_history ORDER BY session_id"))
                ).all() == before
                checks = await db.run_sync(
                    lambda conn: inspect(conn).get_check_constraints("sessions")
                )
                assert any(check["name"] == "ck_sessions_current_activity_pair" for check in checks)
                await db.run_sync(lambda conn: command.check(config(conn)))
                with pytest.raises(NotImplementedError, match="irreversible"):
                    await db.run_sync(lambda conn: command.downgrade(config(conn), "33472a5802a2"))
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as db:
            await db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
async def test_core_migrations_own_only_core_tables(database):
    schema = "tmpv2_migrations_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    try:
        await migrate(settings, "upgrade")
        await migrate(settings, "upgrade")
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(text("CREATE TABLE unrelated (id INTEGER)"))
                await db.execute(text("CREATE TABLE plugin_other_state (id INTEGER)"))
                tables = await db.run_sync(lambda conn: inspect(conn).get_table_names())
                assert set(tables) == (
                    set(ControlTable.metadata.tables) | set(history_metadata.tables)
                ) | {
                    "core_schema_version",
                    "unrelated",
                    "plugin_other_state",
                }

                def check(connection):
                    connection.dialect.default_schema_name = schema
                    config = migration_config(
                        connection,
                        directory=Path(__file__).parents[2] / "src/kapy/database/migrations",
                        metadata=[ControlTable.metadata, history_metadata],
                        version_table="core_schema_version",
                        owns_table=OWNED_TABLES.__contains__,
                    )
                    command.check(config)

                await db.run_sync(check)
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as db:
            await db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
async def test_creating_revision_preserves_legacy_status_and_json(database):
    schema = "tmpv2_creating_migration_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    session_id = uuid4()
    try:
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(CreateSchema(schema))

                def upgrade(connection, target):
                    connection.dialect.default_schema_name = schema
                    config = migration_config(
                        connection,
                        directory=Path(__file__).parents[2] / "src/kapy/database/migrations",
                        metadata=[ControlTable.metadata, history_metadata],
                        version_table="core_schema_version",
                        owns_table=OWNED_TABLES.__contains__,
                    )
                    command.upgrade(config, target)

                await db.run_sync(lambda connection: upgrade(connection, "0618cbf51930"))
                await db.execute(
                    text(
                        "INSERT INTO sessions (id, title, provider_id, model_name, model_settings, "
                        "status, layers, layer_states, compaction_replay_turns, "
                        "created_at, updated_at) "
                        "VALUES (:id, 'retained', :provider, 'test', '{}', 'closing', "
                        '\'{"shell": {"cwd": "/tmp"}}\', '
                        '\'{"shell": {"jobs": {"known": 8}}}\', 10, now(), now())'
                    ),
                    {"id": session_id, "provider": uuid4()},
                )
                await db.execute(
                    text(
                        "INSERT INTO plugin_agent_bindings (session_id, "
                        "plugin_provider, plugin_name, "
                        "data_version, config, state, revision, status, created_at, updated_at) "
                        "VALUES (:id, 'builtin', 'example', 1, '{\"kept\": true}', "
                        "'{\"progress\": 1}', :revision, 'closed', now(), now())"
                    ),
                    {"id": session_id, "revision": uuid4()},
                )
                session = (await db.execute(text("SELECT * FROM sessions"))).one()
                binding = (await db.execute(text("SELECT * FROM plugin_agent_bindings"))).one()
                await db.run_sync(lambda connection: upgrade(connection, "da2b698fbbac"))
                assert (await db.execute(text("SELECT * FROM sessions"))).one() == session
                assert (
                    await db.execute(text("SELECT * FROM plugin_agent_bindings"))
                ).one() == binding
                await db.execute(
                    text("UPDATE sessions SET status='creating' WHERE id=:id"), {"id": session_id}
                )
                assert (
                    await db.execute(text("SELECT status FROM sessions"))
                ).scalar_one() == "creating"
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as db:
            await db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
async def test_shell_scope_state_revision_preserves_other_data_and_reverses_only_identity(database):
    schema = "tmpv2_shell_scope_migration_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    existing_scope = str(uuid4())
    states = [
        ("ready", {"shell": {"jobs": {"a": 8}}, "echo": {"calls": 2}}),
        ("closing", {"shell": {"jobs": {"remaining": 12}}}),
        ("closed", {"shell": {"jobs": {}}}),
        ("ready", {"shell": {"scope_id": existing_scope, "jobs": {"kept": 3}}}),
        ("creating", {}),
        ("closing", {}),
        ("ready", {"echo": {"calls": 4}}),
        ("ready", {}),  # The legacy binding below retains its jobs-only State.
    ]
    session_ids = [uuid4() for _ in states]
    messages = ModelMessagesTypeAdapter.dump_json(
        [ModelRequest([UserPromptPart("kept")], metadata={"seq": 4})]
    ).decode()
    try:
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(CreateSchema(schema))

                def config(connection):
                    connection.dialect.default_schema_name = schema
                    return migration_config(
                        connection,
                        directory=Path(__file__).parents[2] / "src/kapy/database/migrations",
                        metadata=[ControlTable.metadata, history_metadata],
                        version_table="core_schema_version",
                        owns_table=OWNED_TABLES.__contains__,
                    )

                await db.run_sync(
                    lambda connection: command.upgrade(config(connection), "da2b698fbbac")
                )
                for session_id, (status, layer_states) in zip(session_ids, states, strict=True):
                    await db.execute(
                        text(
                            "INSERT INTO sessions (id, provider_id, model_name, title, "
                            "model_settings, status, layers, layer_states, "
                            "compaction_replay_turns, "
                            "runner_state, runner_state_version, created_at, updated_at) "
                            "VALUES (:id, :provider, 'test', 'kept', '{}', :status, "
                            "CAST(:layers AS JSON), CAST(:states AS JSON), 10, :messages, 7, "
                            "'2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z')"
                        ),
                        {
                            "id": session_id,
                            "provider": uuid4(),
                            "status": status,
                            "layers": json.dumps(
                                {"shell": {"cwd": "/tmp"}, "echo": {}}
                                if session_id != session_ids[-1]
                                else {}
                            ),
                            "states": json.dumps(layer_states),
                            "messages": messages,
                        },
                    )
                await db.execute(
                    text(
                        "INSERT INTO plugin_agent_bindings (session_id, plugin_provider, "
                        "plugin_name, data_version, config, state, revision, status, "
                        "created_at, updated_at) VALUES (:id, 'builtin', 'shell', 1, "
                        '\'{"cwd": "/tmp"}\', \'{"jobs": {"legacy": 5}}\', '
                        ":revision, 'ready', now(), now())"
                    ),
                    {"id": session_ids[-1], "revision": uuid4()},
                )
                await db.execute(
                    text(
                        "INSERT INTO agent_history (session_id, seq, kind, message, "
                        "message_metadata, created_at) VALUES (:id, 4, 'request', "
                        '\'{"parts": [{"part_kind": "user-prompt", "content": "kept"}]}\', '
                        '\'{"metadata": {"seq": 4}}\', now())'
                    ),
                    {"id": session_ids[0]},
                )

                async def snapshot():
                    rows = (await db.execute(text("SELECT * FROM sessions"))).mappings().all()
                    return {row["id"]: dict(row) for row in rows}

                before = await snapshot()
                binding = (await db.execute(text("SELECT * FROM plugin_agent_bindings"))).one()
                history = (await db.execute(text("SELECT * FROM agent_history"))).one()
                await db.run_sync(
                    lambda connection: command.upgrade(config(connection), "4a86521aa2ff")
                )
                upgraded = await snapshot()
                for session_id, row in before.items():
                    expected = dict(row)
                    if "shell" in row["layer_states"]:
                        expected["layer_states"] = {
                            **row["layer_states"],
                            "shell": {
                                "scope_id": str(session_id),
                                **row["layer_states"]["shell"],
                            },
                        }
                    assert upgraded[session_id] == expected
                await db.run_sync(
                    lambda connection: command.downgrade(config(connection), "da2b698fbbac")
                )
                downgraded = await snapshot()
                for session_id, row in before.items():
                    expected = dict(row)
                    if "shell" in row["layer_states"]:
                        expected["layer_states"] = {
                            **row["layer_states"],
                            "shell": {
                                key: value
                                for key, value in row["layer_states"]["shell"].items()
                                if key != "scope_id"
                            },
                        }
                    assert downgraded[session_id] == expected
                assert (
                    await db.execute(text("SELECT * FROM plugin_agent_bindings"))
                ).one() == binding
                assert (await db.execute(text("SELECT * FROM agent_history"))).one() == history
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as cleanup:
            await cleanup.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["shell-scope-state", "shell-conversation-state"])
async def test_shell_generation_plan_rejects_unrelated_schema_changes(database, plan):
    schema = "tmpv2_shell_plan_rejection_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    try:
        await migrate(settings, "upgrade")
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(text("ALTER TABLE sessions ADD COLUMN unrelated INTEGER"))
        with pytest.raises(ValueError, match=f"{plan} plan requires no schema changes"):
            await migrate(settings, "revision", "Must reject structural changes", plan=plan)
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as db:
            await db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
async def test_shell_conversation_migration_removes_only_identity_and_restores_session_uuid(
    database,
):
    schema = "shell_conversation_migration_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    states = [
        {"shell": {"scope_id": str(uuid4()), "jobs": {"kept": 17}}, "echo": {"calls": 2}},
        {"shell": {"scope_id": "legacy-identity", "jobs": {}, "extra": {"keep": True}}},
        {"shell": {"jobs": {"already-current": 5}}},
        {},
        {"echo": {"calls": 4}},
        {"shell": None},
        {"shell": []},
        {"shell": "malformed legacy state"},
    ]
    session_ids = [uuid4() for _ in states]
    messages = ModelMessagesTypeAdapter.dump_json(
        [
            ModelRequest(
                [UserPromptPart("kept")],
                metadata={"seq": 4},
                conversation_id="historical-conversation",
            )
        ]
    ).decode()
    try:
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(CreateSchema(schema))

                def config(connection):
                    connection.dialect.default_schema_name = schema
                    return migration_config(
                        connection,
                        directory=Path(__file__).parents[2] / "src/kapy/database/migrations",
                        metadata=[ControlTable.metadata, history_metadata],
                        version_table="core_schema_version",
                        owns_table=OWNED_TABLES.__contains__,
                    )

                await db.run_sync(lambda conn: command.upgrade(config(conn), "978cc41afc77"))
                for session_id, state in zip(session_ids, states, strict=True):
                    await db.execute(
                        text("""INSERT INTO sessions
                        (id, status, title, provider_id, model_name, model_settings,
                         layers, layer_states, runner_state, current_activity_id,
                         current_activity_run_id, created_at, updated_at)
                        VALUES (:id, 'closing', 'kept', :provider, 'test', '{"temperature": 0.5}',
                            '{"shell":{"cwd":"/tmp"},"echo":{}}', CAST(:states AS JSON),
                            :messages, :activity, 'native-run',
                            '2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z')"""),
                        {
                            "id": session_id,
                            "provider": uuid4(),
                            "states": json.dumps(state),
                            "messages": messages,
                            "activity": uuid4(),
                        },
                    )
                await db.execute(
                    text("""INSERT INTO agent_history
                    (session_id, seq, kind, message, message_metadata, created_at)
                    VALUES (:id, 4, 'request', '{"parts":[]}',
                        CAST(:metadata AS JSON),
                        '2026-09-01T00:00:00Z')"""),
                    {
                        "id": session_ids[0],
                        "metadata": json.dumps(
                            {"conversation_id": "historical-conversation", "metadata": {"seq": 4}}
                        ),
                    },
                )

                async def snapshot():
                    rows = (await db.execute(text("SELECT * FROM sessions"))).mappings().all()
                    return {row["id"]: dict(row) for row in rows}

                before = await snapshot()
                history = (await db.execute(text("SELECT * FROM agent_history"))).all()
                await db.run_sync(lambda conn: command.upgrade(config(conn), "head"))
                await db.run_sync(lambda conn: command.upgrade(config(conn), "head"))
                upgraded = await snapshot()
                for session_id, row in before.items():
                    expected = dict(row)
                    shell = row["layer_states"].get("shell")
                    if isinstance(shell, dict):
                        expected["layer_states"] = {
                            **row["layer_states"],
                            "shell": {
                                key: value for key, value in shell.items() if key != "scope_id"
                            },
                        }
                    assert upgraded[session_id] == expected

                await db.run_sync(lambda conn: command.downgrade(config(conn), "978cc41afc77"))
                downgraded = await snapshot()
                for session_id, row in before.items():
                    expected = dict(row)
                    shell = row["layer_states"].get("shell")
                    if isinstance(shell, dict):
                        expected["layer_states"] = {
                            **row["layer_states"],
                            "shell": {**shell, "scope_id": str(session_id)},
                        }
                    assert downgraded[session_id] == expected
                await db.run_sync(lambda conn: command.upgrade(config(conn), "head"))
                assert await snapshot() == upgraded
                assert (await db.execute(text("SELECT * FROM agent_history"))).all() == history
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as cleanup:
            await cleanup.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
async def test_core_migrations_preserve_pages_and_execution(database):
    schema = "tmpv2_page_migration_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    session_id = uuid4()
    try:
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(CreateSchema(schema))

                def migrate_to(connection, target, *, downgrade=False):
                    connection.dialect.default_schema_name = schema
                    config = migration_config(
                        connection,
                        directory=Path(__file__).parents[2] / "src/kapy/database/migrations",
                        metadata=[ControlTable.metadata, history_metadata],
                        version_table="core_schema_version",
                        owns_table=OWNED_TABLES.__contains__,
                    )
                    (command.downgrade if downgrade else command.upgrade)(config, target)

                await db.run_sync(lambda connection: migrate_to(connection, "12fd53fd60d5"))
                await db.execute(
                    text(
                        "INSERT INTO agent_compactions "
                        "(session_id, last_message_seq, text, created_at) "
                        "VALUES (:session, 9, :summary, '2026-09-01T00:00:00Z')"
                    ),
                    {"session": session_id, "summary": "summary with Unicode 摘要"},
                )
                await db.run_sync(lambda connection: migrate_to(connection, "eae2fdb48969"))
                row = (await db.execute(text("SELECT * FROM agent_context_pages"))).one()
                assert row.session_id == session_id and row.anchor_seq == 9
                assert row.policy_key == "summary/v1"
                assert row.payload == {"summary": "summary with Unicode 摘要"}
                assert row.created_at.isoformat() == "2026-09-01T00:00:00+00:00"
                await db.run_sync(
                    lambda connection: migrate_to(connection, "12fd53fd60d5", downgrade=True)
                )
                old = (await db.execute(text("SELECT * FROM agent_compactions"))).one()
                assert old.last_message_seq == 9 and old.text == row.payload["summary"]
                assert old.created_at == row.created_at
                await db.run_sync(lambda connection: migrate_to(connection, "eae2fdb48969"))
                await db.execute(
                    text(
                        "INSERT INTO agent_states (session_id, next_step) "
                        "VALUES (:session, 'model_request')"
                    ),
                    {"session": session_id},
                )
                await db.execute(
                    text(
                        "INSERT INTO agent_history "
                        "(session_id, seq, kind, message, message_metadata) "
                        "VALUES (:session, 10, 'request', "
                        "'{\"parts\": []}', '{}')"
                    ),
                    {"session": session_id},
                )
                await db.execute(
                    text(
                        "INSERT INTO sessions (id, title, provider_id, model_name, model_settings, "
                        "compaction_replay_turns, created_at, updated_at) "
                        "VALUES (:session, 'existing', :provider, 'test', '{}', 10, now(), now())"
                    ),
                    {"session": session_id, "provider": uuid4()},
                )
                # Upgrade from the deployed context-pages head, preserving both
                # the page and pending execution while moving ownership storage.
                await db.run_sync(lambda connection: migrate_to(connection, "da2b698fbbac"))
                assert (await db.execute(text("SELECT * FROM agent_context_pages"))).one() == row
                assert (
                    await db.execute(text("SELECT context_plugin FROM sessions"))
                ).scalar_one() == {
                    "name": "kapy/summary",
                    "config": {},
                }
                assert (
                    await db.execute(
                        text("SELECT runner_state, runner_state_version FROM sessions")
                    )
                ).one() == (None, 0)
                assert (
                    await db.execute(text("SELECT next_step FROM agent_states"))
                ).scalar_one() == "model_request"
                assert (await db.execute(text("SELECT seq FROM agent_history"))).scalar_one() == 10
                assert (
                    await db.execute(text("SELECT count(*) FROM session_leases"))
                ).scalar_one() == 0
                columns = await db.run_sync(
                    lambda connection: inspect(connection).get_columns("agent_states")
                )
                assert {column["name"] for column in columns}.isdisjoint(
                    {"lock_token", "heartbeat_at"}
                )
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as db:
            await db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
async def test_offline_history_migration_preserves_recorded_messages_and_unmarks_removed_tail(
    database,
):
    schema = "tmpv2_immutable_migration_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    durable_id, legacy_id = uuid4(), uuid4()
    histories = {
        durable_id: [
            ModelRequest(
                [UserPromptPart("confirmed")],
                metadata={
                    "seq": 0,
                    "authoritative": True,
                    "source": "input",
                    "business": {"id": 7},
                },
                instructions="Keep request instructions",
                run_id="migration-run",
            ),
            ModelResponse(
                [TextPart("reply")],
                metadata={"seq": 2, "authoritative": True, "source": "response"},
                model_name="stored-model",
                provider_name="stored-provider",
                provider_url="https://provider.invalid/v1",
                provider_response_id="stored-response",
                provider_details={"request_id": "provider-request"},
            ),
            # Storage columns decide removal even if SDK metadata claims authority.
            ModelRequest([UserPromptPart("pending")], metadata={"seq": 3, "authoritative": True}),
            ModelResponse(
                [TextPart("sent")],
                metadata={"seq": 4, "authoritative": False, "source": "already-sent"},
                provider_details={"kept": [1, "value"]},
            ),
            ModelRequest(
                [UserPromptPart("tail")], metadata={"authoritative": False, "source": "hook"}
            ),
        ],
        legacy_id: [
            ModelRequest(
                [UserPromptPart("legacy")],
                metadata={"seq": 0, "authoritative": False, "source": "legacy"},
                instructions="Keep legacy instructions",
            ),
            ModelResponse([TextPart("kept")], metadata={"seq": 1, "authoritative": False}),
        ],
    }
    try:
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(CreateSchema(schema))

                def config(connection):
                    connection.dialect.default_schema_name = schema
                    return migration_config(
                        connection,
                        directory=Path(__file__).parents[2] / "src/kapy/database/migrations",
                        metadata=[ControlTable.metadata, history_metadata],
                        version_table="core_schema_version",
                        owns_table=OWNED_TABLES.__contains__,
                    )

                await db.run_sync(
                    lambda connection: command.upgrade(config(connection), "f0e75989c93b")
                )
                for session_id, messages in histories.items():
                    await db.execute(
                        text(
                            "INSERT INTO sessions (id, provider_id, model_name, title, "
                            "model_settings, compaction_replay_turns, runner_state, "
                            "runner_state_version, created_at, updated_at) "
                            "VALUES (:id, :provider, 'test', 'kept', '{}', 10, :state, 7, "
                            "'2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z')"
                        ),
                        {
                            "id": session_id,
                            "provider": uuid4(),
                            "state": ModelMessagesTypeAdapter.dump_json(messages).decode(),
                        },
                    )
                    for message in messages:
                        seq = (message.metadata or {}).get("seq")
                        if seq is None:
                            continue
                        payload = ModelMessagesTypeAdapter.dump_python([message], mode="json")[0]
                        kind, parts = payload.pop("kind"), payload.pop("parts")
                        payload.pop("usage", None)
                        payload.pop("finish_reason", None)
                        await db.execute(
                            text(
                                "INSERT INTO agent_history (session_id, seq, authoritative, kind, "
                                "message, message_metadata, created_at) "
                                "VALUES (:session, :seq, :authority, :kind, "
                                "CAST(:message AS JSON), "
                                "CAST(:metadata AS JSON), '2026-09-01T00:00:00Z')"
                            ),
                            {
                                "session": session_id,
                                "seq": seq,
                                "authority": session_id == durable_id and seq in {0, 2},
                                "kind": kind,
                                "message": json.dumps({"parts": parts}),
                                "metadata": json.dumps(payload),
                            },
                        )
                await db.execute(
                    text("INSERT INTO agent_states (session_id, next_step) VALUES (:id, 'done')"),
                    {"id": legacy_id},
                )
                before_metadata = {
                    (row.session_id, row.seq): row.message_metadata
                    for row in (await db.execute(text("SELECT * FROM agent_history"))).all()
                }
                await db.run_sync(
                    lambda connection: command.upgrade(config(connection), "b1d9c9e540c5")
                )
                rows = (
                    await db.execute(text("SELECT * FROM agent_history ORDER BY session_id, seq"))
                ).all()
                assert [row.seq for row in rows if row.session_id == durable_id] == [0, 2, 4]
                assert [row.seq for row in rows if row.session_id == legacy_id] == [0, 1]
                for row in rows:
                    assert row.created_at.isoformat() == "2026-09-01T00:00:00+00:00"
                    expected = next(
                        m
                        for m in histories[row.session_id]
                        if (m.metadata or {}).get("seq") == row.seq
                    )
                    assert row.message == {
                        "parts": ModelMessagesTypeAdapter.dump_python([expected], mode="json")[0][
                            "parts"
                        ]
                    }
                    expected_metadata = before_metadata[row.session_id, row.seq]
                    expected_metadata["metadata"].pop("authoritative")
                    assert row.message_metadata == expected_metadata
                states = (
                    await db.execute(
                        text(
                            "SELECT id, runner_state, runner_state_version, updated_at "
                            "FROM sessions"
                        )
                    )
                ).all()
                for row in states:
                    restored = ModelMessagesTypeAdapter.validate_json(row.runner_state)
                    for before, after in zip(histories[row.id], restored, strict=True):
                        expected_metadata = dict(before.metadata or {})
                        expected_metadata.pop("authoritative", None)
                        if row.id == durable_id and expected_metadata.get("seq") == 3:
                            expected_metadata.pop("seq")
                        before.metadata = expected_metadata
                        assert after == before
                    assert row.runner_state_version == 7
                    assert row.updated_at.isoformat() == "2026-09-02T00:00:00+00:00"
                with pytest.raises(NotImplementedError, match="irreversible"):
                    await db.run_sync(
                        lambda connection: command.downgrade(config(connection), "f0e75989c93b")
                    )
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as db:
            await db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.mark.asyncio
async def test_operation_migration_fences_legacy_sessions_and_preserves_stored_data(database):
    schema = "operation_migration_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    identities = {phase: uuid4() for phase in ("creating", "ready", "closing", "closed")}
    try:
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(CreateSchema(schema))

                def config(connection):
                    connection.dialect.default_schema_name = schema
                    return migration_config(
                        connection,
                        directory=Path(__file__).parents[2] / "src/kapy/database/migrations",
                        metadata=[ControlTable.metadata, history_metadata],
                        version_table="core_schema_version",
                        owns_table=OWNED_TABLES.__contains__,
                    )

                await db.run_sync(lambda conn: command.upgrade(config(conn), "5be81e905907"))
                for phase, session_id in identities.items():
                    await db.execute(
                        text("""INSERT INTO sessions
                        (id, status, title, provider_id, model_name, model_settings, layers,
                         layer_states, runner_state, runner_state_version, created_at, updated_at)
                        VALUES (:id, :phase, 'retained', :provider, 'test', '{}', '{}', '{}',
                                'opaque snapshot', 7, now(), now())"""),
                        {"id": session_id, "phase": phase, "provider": uuid4()},
                    )
                    await db.execute(
                        text("""INSERT INTO agent_history
                        (session_id, seq, kind, message, message_metadata, created_at)
                        VALUES (:id, 3, 'request', '{"parts":[]}', '{}', now())"""),
                        {"id": session_id},
                    )
                before = (
                    await db.execute(text("SELECT * FROM agent_history ORDER BY session_id"))
                ).all()
                await db.run_sync(lambda conn: command.upgrade(config(conn), "head"))
                rows = (await db.execute(text("SELECT * FROM sessions"))).all()
                assert {row.id: row.status for row in rows} == {
                    sid: "closed" if phase == "closed" else "closing"
                    for phase, sid in identities.items()
                }
                assert all(
                    row.runner_state == "opaque snapshot" and row.current_activity_id is None
                    for row in rows
                )
                assert (
                    await db.execute(text("SELECT * FROM agent_history ORDER BY session_id"))
                ).all() == before
                assert "session_operations" not in await db.run_sync(
                    lambda conn: inspect(conn).get_table_names()
                )
                assert "session_leases" not in await db.run_sync(
                    lambda conn: inspect(conn).get_table_names()
                )
                assert "runner_state_version" not in {
                    column["name"]
                    for column in await db.run_sync(
                        lambda conn: inspect(conn).get_columns("sessions")
                    )
                }
                await db.run_sync(lambda conn: command.check(config(conn)))
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as db:
            await db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
