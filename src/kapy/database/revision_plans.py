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
