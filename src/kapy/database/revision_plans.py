"""Explicit, reviewable generation inputs for changes autogenerate cannot infer.

These plans run only while producing a revision. Generated revisions contain the
fixed operations and have no dependency on this module or current ORM models.
"""

import sqlalchemy as sa
from alembic.operations import ops


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
