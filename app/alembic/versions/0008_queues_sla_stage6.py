"""stage 6 queues + SLA foundation: enum types, organizations.timezone +
business_hours, urgency_levels.respects_business_hours.

Only the two enum types are created here, via the same raw `op.execute`
idiom as 0001/0002 (`sa.Enum(...).create(..., checkfirst=True)` needs a
live connection and breaks offline `--sql` generation). The consuming
`sla_events`/`notifications` tables belong to a later PR, once their
models exist. (Note: `ticket_channel`, created in 0001, is consumed by a
column in that same migration — it is not a precedent for a type created
ahead of its consumer. This split is justified on its own: it keeps this
PR under the review budget and each migration mapped to one PR's scope.)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-21

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- new enum types (consuming tables come in a later PR) -----------
    op.execute("CREATE TYPE sla_event_type AS ENUM ('warning', 'breached', 'resolved')")
    op.execute(
        "CREATE TYPE notification_kind AS ENUM ('sla_warning', 'sla_breached')"
    )

    # --- organizations: business-hours configuration ---------------------
    op.add_column(
        "organizations",
        sa.Column(
            "timezone",
            sa.String(),
            nullable=False,
            server_default="America/Argentina/Buenos_Aires",
        ),
    )
    # Inline literal — must byte-match `sla_defaults.DEFAULT_BUSINESS_HOURS`;
    # enforced by `tests/unit/test_sla_defaults_migration_parity.py`.
    op.add_column(
        "organizations",
        sa.Column(
            "business_hours",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text(
                '\'{"mon":[["09:00","18:00"]],"tue":[["09:00","18:00"]],'
                '"wed":[["09:00","18:00"]],"thu":[["09:00","18:00"]],'
                '"fri":[["09:00","18:00"]]}\'::jsonb'
            ),
        ),
    )
    op.create_check_constraint(
        "ck_organizations_timezone_not_blank", "organizations", "btrim(timezone) <> ''"
    )

    # --- urgency_levels: business-hours-aware SLA clock -------------------
    op.add_column(
        "urgency_levels",
        sa.Column(
            "respects_business_hours",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.execute(
        "UPDATE urgency_levels SET respects_business_hours = false "
        "WHERE lower(btrim(name)) IN ('critical', 'high')"
    )


def downgrade() -> None:
    op.drop_column("urgency_levels", "respects_business_hours")

    op.drop_constraint("ck_organizations_timezone_not_blank", "organizations", type_="check")
    op.drop_column("organizations", "business_hours")
    op.drop_column("organizations", "timezone")

    op.execute("DROP TYPE notification_kind")
    op.execute("DROP TYPE sla_event_type")
