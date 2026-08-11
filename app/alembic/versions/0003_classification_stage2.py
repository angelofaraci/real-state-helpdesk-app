"""classification stage 2 foundation: ticket title/description, optional
taxonomy on tickets, urgency prediction on classifications

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-11

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. tickets.title (backfilled NOT NULL) and tickets.description (nullable).
    op.add_column("tickets", sa.Column("title", sa.String(), nullable=True))
    op.execute("UPDATE tickets SET title = 'Untitled ticket' WHERE title IS NULL")
    op.alter_column("tickets", "title", nullable=False)
    op.add_column("tickets", sa.Column("description", sa.Text(), nullable=True))

    # 2. Title must not be blank once populated.
    op.create_check_constraint(
        "ck_tickets_title_not_blank", "tickets", "btrim(title) <> ''"
    )

    # 3. category_id/urgency_id/sla_due_at become optional: a ticket may
    # exist before classification has assigned taxonomy.
    op.alter_column("tickets", "category_id", nullable=True)
    op.alter_column("tickets", "urgency_id", nullable=True)
    op.alter_column("tickets", "sla_due_at", nullable=True)

    # 4. Once classified, taxonomy must be present. Safe: migration 0002
    # backfilled every existing row to classified with non-null taxonomy.
    op.create_check_constraint(
        "ck_tickets_classified_has_taxonomy",
        "tickets",
        "classification_status <> 'classified' OR "
        "(category_id IS NOT NULL AND urgency_id IS NOT NULL AND sla_due_at IS NOT NULL)",
    )

    # 5. urgency_levels.description (nullable).
    op.add_column(
        "urgency_levels", sa.Column("description", sa.String(), nullable=True)
    )

    # 6. classifications: predicted_urgency + urgency_confidence.
    op.add_column(
        "classifications", sa.Column("predicted_urgency", sa.String(), nullable=True)
    )
    op.add_column(
        "classifications",
        sa.Column("urgency_confidence", sa.Double(), nullable=True),
    )
    op.create_check_constraint(
        "ck_classifications_urgency_confidence_range",
        "classifications",
        "urgency_confidence IS NULL OR (urgency_confidence BETWEEN 0 AND 1)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_classifications_urgency_confidence_range",
        "classifications",
        type_="check",
    )
    op.drop_column("classifications", "urgency_confidence")
    op.drop_column("classifications", "predicted_urgency")

    op.drop_column("urgency_levels", "description")

    op.drop_constraint("ck_tickets_classified_has_taxonomy", "tickets", type_="check")

    op.alter_column("tickets", "sla_due_at", nullable=False)
    op.alter_column("tickets", "urgency_id", nullable=False)
    op.alter_column("tickets", "category_id", nullable=False)

    op.drop_constraint("ck_tickets_title_not_blank", "tickets", type_="check")

    op.drop_column("tickets", "description")
    op.alter_column("tickets", "title", nullable=True)
    op.drop_column("tickets", "title")
