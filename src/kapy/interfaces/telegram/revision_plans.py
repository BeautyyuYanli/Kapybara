"""Offline Telegram conversion inputs; generated revisions contain fixed SQL only."""

from alembic.operations import ops


def immutable_history(context, revision, directives) -> None:
    """Fold delivered receipts into the cursor and retain unfinished send offsets.

    Acknowledged receipts and obsolete edit state are discarded, so the generated
    downgrade rejects execution instead of restoring misleading empty state.
    """
    script = directives[0]
    if len(script.upgrade_ops.ops) != 1 or not isinstance(
        table := script.upgrade_ops.ops[0], ops.ModifyTableOps
    ):
        raise ValueError("immutable-history plan requires only Telegram receipt removal")
    if (
        table.table_name != "plugin_telegram_delivery"
        or len(table.ops) != 1
        or not isinstance(table.ops[0], ops.DropColumnOp)
        or table.ops[0].column_name != "sent_responses"
    ):
        raise ValueError("immutable-history plan requires only Telegram receipt removal")
    script.upgrade_ops.ops[:0] = [
        ops.ExecuteSQLOp("""UPDATE plugin_telegram_delivery
SET after_seq = max(after_seq, COALESCE(
    (SELECT max(CAST(key AS INTEGER)) FROM json_each(sent_responses)), after_seq
))"""),
        ops.ExecuteSQLOp("""UPDATE plugin_telegram_delivery
SET pending = NULL, item_offset = 0, next_attempt_at = 0, blocked_error = NULL
WHERE pending IS NOT NULL AND CAST(json_extract(pending, '$.seq') AS INTEGER) <= after_seq"""),
        ops.ExecuteSQLOp("""UPDATE plugin_telegram_delivery
SET pending = json_remove(pending, '$.authoritative', '$.message_ids', '$.item_index')
WHERE pending IS NOT NULL"""),
    ]
    script.downgrade_ops = ops.DowngradeOps([])
    context.config.attributes["irreversible_reason"] = "Telegram receipt conversion is irreversible"
