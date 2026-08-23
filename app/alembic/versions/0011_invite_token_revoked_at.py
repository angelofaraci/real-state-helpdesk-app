"""stage 8 devops: invite token revoke-not-delete.

Adds a nullable `revoked_at` column to `invite_tokens` and widens the
existing "single outstanding invite per user" partial unique index
(`ix_invite_tokens_user_id_single_outstanding`) from
`WHERE used_at IS NULL` to `WHERE used_at IS NULL AND revoked_at IS NULL`.

This is a plain blocking `DROP INDEX` + `CREATE UNIQUE INDEX` inside
Alembic's standard transaction — not `CREATE INDEX CONCURRENTLY` — because
`invite_tokens` is a small table and atomicity is preferred over avoiding
a brief write lock (design decision for this stage).

Backward compatibility (load-bearing for this stage's deploy-rollback
design, where an image rollback does NOT roll back migrations): the OLD
application code (pre-this-PR, unaware of `revoked_at`) can still run
unmodified against the NEW schema. `revoked_at` is nullable with no
default change required, and the widened predicate `used_at IS NULL AND
revoked_at IS NULL` indexes a strict subset of the rows the old predicate
`used_at IS NULL` indexed — every row the old predicate would have
rejected as a duplicate is still rejected, and old code never sets
`revoked_at`, so old code observes identical index behavior to before.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-23

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invite_tokens",
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.drop_index(
        "ix_invite_tokens_user_id_single_outstanding", table_name="invite_tokens"
    )
    op.create_index(
        "ix_invite_tokens_user_id_single_outstanding",
        "invite_tokens",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_invite_tokens_user_id_single_outstanding", table_name="invite_tokens"
    )
    op.create_index(
        "ix_invite_tokens_user_id_single_outstanding",
        "invite_tokens",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL"),
    )
    op.drop_column("invite_tokens", "revoked_at")
