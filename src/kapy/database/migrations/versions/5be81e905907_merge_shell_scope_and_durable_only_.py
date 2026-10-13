"""Merge shell scope and durable-only migrations

Revision ID: 5be81e905907
Revises: 4a86521aa2ff, 5de14eebe20b
"""

# Alembic renders SQL statements as indivisible string literals.
# ruff: noqa: E501

from collections.abc import Sequence

revision: str = "5be81e905907"
down_revision: str | Sequence[str] | None = ("4a86521aa2ff", "5de14eebe20b")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
