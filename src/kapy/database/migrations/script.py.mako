"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""

# Alembic renders SQL statements as indivisible string literals.
# ruff: noqa: E501

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
% if config.attributes.get("irreversible_reason"):
    raise NotImplementedError(${repr(config.attributes["irreversible_reason"])})
% else:
    ${downgrades if downgrades else "pass"}
% endif
