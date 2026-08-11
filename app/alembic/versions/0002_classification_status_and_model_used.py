"""classification status and model_used

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-11

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CLASSIFICATION_STATUS_VALUES = ["pending", "classified"]


def _classification_status_enum() -> postgresql.ENUM:
    return postgresql.ENUM(
        *_CLASSIFICATION_STATUS_VALUES, name="classification_status", create_type=False
    )


def upgrade() -> None:
    op.execute("CREATE TYPE classification_status AS ENUM ('pending', 'classified')")

    op.add_column(
        "tickets",
        sa.Column(
            "classification_status",
            _classification_status_enum(),
            nullable=False,
            server_default="pending",
        ),
    )
    # Backfill: every pre-existing ticket already has category_id NOT NULL,
    # so all existing rows are already effectively classified.
    op.execute("UPDATE tickets SET classification_status = 'classified'")

    op.create_index(
        "ix_tickets_organization_id_classification_status",
        "tickets",
        ["organization_id", "classification_status"],
    )

    op.add_column(
        "classifications",
        sa.Column("model_used", sa.String(), nullable=True),
    )
    op.create_check_constraint(
        "ck_classifications_model_used_known",
        "classifications",
        "model_used IS NULL OR model_used IN ('sklearn_v1', 'llm_fallback')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_classifications_model_used_known", "classifications", type_="check"
    )
    op.drop_column("classifications", "model_used")

    op.drop_index("ix_tickets_organization_id_classification_status", table_name="tickets")
    op.drop_column("tickets", "classification_status")

    op.execute("DROP TYPE classification_status")
