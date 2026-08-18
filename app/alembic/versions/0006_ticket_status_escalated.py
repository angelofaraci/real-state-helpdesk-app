"""add 'escalated' value to the existing ticket_status enum (stage 4 —
chatbot: a ticket created when the chatbot hands a conversation off to a
human agent)

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-18

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block in
    # Postgres, so it must be issued in an autocommit block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE ticket_status ADD VALUE IF NOT EXISTS 'escalated'")


def downgrade() -> None:
    # Postgres has no `ALTER TYPE ... DROP VALUE`: an enum value, once
    # added, cannot be removed without recreating the type (rewriting every
    # dependent column), which is far too destructive for an automatic
    # downgrade. This is a documented, intentional no-op — downgrading past
    # this revision leaves the 'escalated' label in place.
    pass
