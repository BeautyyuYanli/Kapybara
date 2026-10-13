"""Explicit, reviewable generation inputs for changes autogenerate cannot infer.

These plans run only while producing a revision. Generated revisions contain the
fixed operations and have no dependency on this module or current ORM models.
"""

import sqlalchemy as sa
from alembic.operations import ops


def shell_scope_state(context, revision, directives) -> None:
    """Backfill only existing shell State; creating/abandoned empty State stays empty.

    This data-only revision requires no schema differences. It does not rewrite
    runner snapshots, their versions or recorded Temporal payloads.
    """
    script = directives[0]
    if script.upgrade_ops.ops or script.downgrade_ops.ops:
        raise ValueError("shell-scope-state plan requires no schema changes")
    script.upgrade_ops = ops.UpgradeOps(
        [
            ops.ExecuteSQLOp("""UPDATE sessions
SET layer_states = jsonb_set(layer_states::jsonb, '{shell,scope_id}',
    to_jsonb(id::text))::json
WHERE jsonb_typeof(layer_states::jsonb->'shell') = 'object'
    AND NOT (layer_states::jsonb->'shell' ? 'scope_id')"""),
        ]
    )
    script.downgrade_ops = ops.DowngradeOps(
        [
            ops.ExecuteSQLOp("""UPDATE sessions
SET layer_states = jsonb_set(layer_states::jsonb, '{shell}',
    (layer_states::jsonb->'shell') - 'scope_id')::json
WHERE jsonb_typeof(layer_states::jsonb->'shell') = 'object'
    AND layer_states::jsonb->'shell' ? 'scope_id'"""),
        ]
    )


def context_pages(context, revision, directives) -> None:
    """Replace only the expected summary drop/page create with a data-preserving rename."""
    script = directives[0]
    detected = {
        (type(operation), getattr(operation, "table_name", None))
        for operation in script.upgrade_ops.ops
    }
    expected = {
        (ops.DropTableOp, "agent_compactions"),
        (ops.CreateTableOp, "agent_context_pages"),
    }
    if detected != expected:
        raise ValueError("context-pages plan requires exactly the summary-to-page schema change")
    script.upgrade_ops = ops.UpgradeOps(
        [
            ops.ExecuteSQLOp("ALTER TABLE agent_compactions RENAME TO agent_context_pages"),
            ops.AlterColumnOp(
                "agent_context_pages",
                "last_message_seq",
                modify_name="anchor_seq",
                existing_type=sa.Integer(),
            ),
            ops.AddColumnOp(
                "agent_context_pages", sa.Column("policy_key", sa.Text(), nullable=True)
            ),
            ops.AddColumnOp("agent_context_pages", sa.Column("payload", sa.JSON(), nullable=True)),
            ops.ExecuteSQLOp(
                "UPDATE agent_context_pages SET policy_key = 'summary/v1', "
                "payload = json_build_object('summary', text)"
            ),
            ops.AlterColumnOp(
                "agent_context_pages", "policy_key", modify_nullable=False, existing_type=sa.Text()
            ),
            ops.AlterColumnOp(
                "agent_context_pages", "payload", modify_nullable=False, existing_type=sa.JSON()
            ),
            ops.DropColumnOp("agent_context_pages", "text"),
        ]
    )
    script.downgrade_ops = ops.DowngradeOps(
        [
            ops.ExecuteSQLOp("""DO $$ BEGIN
IF EXISTS (SELECT 1 FROM agent_context_pages WHERE policy_key <> 'summary/v1'
OR json_typeof(payload->'summary') IS DISTINCT FROM 'string') THEN
RAISE EXCEPTION 'Only summary/v1 pages can be downgraded';
END IF; END $$"""),
            ops.AddColumnOp("agent_context_pages", sa.Column("text", sa.Text(), nullable=True)),
            ops.ExecuteSQLOp("UPDATE agent_context_pages SET text = payload->>'summary'"),
            ops.AlterColumnOp(
                "agent_context_pages", "text", modify_nullable=False, existing_type=sa.Text()
            ),
            ops.DropColumnOp("agent_context_pages", "payload"),
            ops.DropColumnOp("agent_context_pages", "policy_key"),
            ops.AlterColumnOp(
                "agent_context_pages",
                "anchor_seq",
                modify_name="last_message_seq",
                existing_type=sa.Integer(),
            ),
            ops.ExecuteSQLOp("ALTER TABLE agent_context_pages RENAME TO agent_compactions"),
        ]
    )


def immutable_history(context, revision, directives) -> None:
    """Convert offline history/state before dropping the obsolete authority schema.

    Only unconfirmed durable request rows are removed. Their positions are also
    unmarked in runner_state while their SDK content remains available. The data
    conversion cannot be reversed, so the generated downgrade rejects execution.
    """
    script = directives[0]
    if len(script.upgrade_ops.ops) != 1 or not isinstance(
        table := script.upgrade_ops.ops[0], ops.ModifyTableOps
    ):
        raise ValueError("immutable-history plan requires only the history authority removal")
    detected = {
        (
            type(operation),
            getattr(operation, "column_name", None),
            getattr(operation, "index_name", None),
        )
        for operation in table.ops
    }
    expected = {
        (ops.DropColumnOp, "authoritative", None),
        (ops.DropIndexOp, None, "ix_agent_history_session_authoritative_seq"),
    }
    if table.table_name != "agent_history" or detected != expected:
        raise ValueError("immutable-history plan requires only the history authority removal")
    script.upgrade_ops.ops[:0] = [
        ops.ExecuteSQLOp("""UPDATE sessions AS s
SET runner_state = (
    SELECT COALESCE(jsonb_agg(
        CASE WHEN jsonb_typeof(m.value->'metadata') = 'object' THEN
            jsonb_set(m.value, '{metadata}',
                CASE WHEN EXISTS (
                    SELECT 1 FROM agent_history h
                    WHERE h.session_id = s.id AND NOT h.authoritative
                        AND h.kind = 'request'
                        AND m.value->'metadata'->>'seq' = h.seq::text
                        AND NOT EXISTS (
                            SELECT 1 FROM agent_states a WHERE a.session_id = h.session_id
                        )
                ) THEN (m.value->'metadata') - 'seq' - 'authoritative'
                ELSE (m.value->'metadata') - 'authoritative' END)
        ELSE m.value END ORDER BY m.ordinality
    ), '[]'::jsonb)::text
    FROM jsonb_array_elements(s.runner_state::jsonb) WITH ORDINALITY AS m
)
WHERE runner_state IS NOT NULL"""),
        ops.ExecuteSQLOp("""DELETE FROM agent_history h
WHERE NOT h.authoritative AND h.kind = 'request'
    AND NOT EXISTS (SELECT 1 FROM agent_states a WHERE a.session_id = h.session_id)"""),
        ops.ExecuteSQLOp("""UPDATE agent_history
SET message_metadata = jsonb_set(message_metadata::jsonb, '{metadata}',
    (message_metadata::jsonb->'metadata') - 'authoritative')::json
WHERE jsonb_typeof(message_metadata::jsonb->'metadata') = 'object'
    AND message_metadata::jsonb->'metadata' ? 'authoritative'"""),
    ]
    script.downgrade_ops = ops.DowngradeOps([])
    context.config.attributes["irreversible_reason"] = (
        "Immutable history conversion is irreversible"
    )


def durable_only(context, revision, directives) -> None:
    """Close incompatible sessions before discarding their legacy recovery data.

    No snapshot is fabricated from history. Durable snapshots, versions and every
    history row remain intact. Run offline after cleaning plugin resources with
    the preceding release; this migration cannot execute removed business hooks.
    """
    script = directives[0]
    removed_tables = {
        "agent_states",
        "agent_context_pages",
        "plugin_agent_bindings",
        "session_inputs",
        "session_cancels",
    }
    detected_tables: set[str] = set()
    detected_columns: set[str] = set()
    for operation in script.upgrade_ops.ops:
        if isinstance(operation, ops.DropTableOp) and operation.table_name in removed_tables:
            detected_tables.add(operation.table_name)
        elif isinstance(operation, ops.ModifyTableOps):
            for change in operation.ops:
                if (
                    operation.table_name == "sessions"
                    and isinstance(change, ops.DropColumnOp)
                    and change.column_name
                    in {"context_plugin", "compaction_threshold_tokens", "compaction_replay_turns"}
                ):
                    detected_columns.add(change.column_name)
                elif operation.table_name in removed_tables and isinstance(change, ops.DropIndexOp):
                    continue
                else:
                    raise ValueError("durable-only plan requires only the legacy schema removal")
        else:
            raise ValueError("durable-only plan requires only the legacy schema removal")
    if detected_tables != removed_tables or detected_columns != {
        "context_plugin",
        "compaction_threshold_tokens",
        "compaction_replay_turns",
    }:
        raise ValueError("durable-only plan requires the complete legacy schema removal")
    constraints = sa.inspect(context.connection).get_check_constraints("sessions")
    obsolete_checks = [
        constraint
        for constraint in constraints
        if any(
            column in constraint["sqltext"]
            for column in ("compaction_threshold_tokens", "compaction_replay_turns")
        )
    ]
    if len(obsolete_checks) != 2 or any(not check["name"] for check in obsolete_checks):
        raise ValueError("durable-only plan requires both named legacy compaction checks")
    for operation in script.upgrade_ops.ops:
        if isinstance(operation, ops.ModifyTableOps) and operation.table_name == "sessions":
            operation.ops[:0] = [
                ops.DropConstraintOp(check["name"], "sessions", type_="check")
                for check in obsolete_checks
            ]
    script.upgrade_ops.ops.insert(
        0,
        ops.ExecuteSQLOp("""UPDATE sessions AS s
SET status = 'closed', updated_at = clock_timestamp()
WHERE EXISTS (SELECT 1 FROM agent_states a WHERE a.session_id = s.id)
    OR EXISTS (SELECT 1 FROM plugin_agent_bindings b WHERE b.session_id = s.id)"""),
    )
    script.downgrade_ops = ops.DowngradeOps([])
    context.config.attributes["irreversible_reason"] = (
        "Legacy runner recovery and plugin state removal is irreversible"
    )


def session_operations(context, revision, directives) -> None:
    """Fence legacy ownership before removing leases and snapshot versions."""
    script = directives[0]
    script.upgrade_ops.ops.insert(
        0,
        ops.ExecuteSQLOp(
            "UPDATE sessions SET status = 'closing', updated_at = clock_timestamp() "
            "WHERE status <> 'closed'"
        ),
    )
    script.downgrade_ops = ops.DowngradeOps([])
    context.config.attributes["irreversible_reason"] = (
        "Session operation ownership migration is irreversible"
    )
