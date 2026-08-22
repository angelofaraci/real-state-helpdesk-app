"""Default SLA business-hours seed data (stage 6 — queues + SLA). Every
organization is bootstrapped with these business hours at creation time —
mirrors `app.core.taxonomy_defaults`'s pattern.

Deliberately pure data with NO session/repository imports: the SLA compute
core (a later PR) can import this shape without pulling in the service
layer, and the migration that backfills `organizations.business_hours`
(`app/alembic/versions/0008_queues_sla_stage6.py`) embeds this exact same
JSON shape inline — see
`tests/unit/test_sla_defaults_migration_parity.py` for the drift guard
between the two.
"""

from __future__ import annotations

DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"

# Mon-Fri, 09:00-18:00. Sat/sun keys are deliberately absent — absence means
# "closed", not an empty list of intervals.
DEFAULT_BUSINESS_HOURS: dict[str, list[list[str]]] = {
    "mon": [["09:00", "18:00"]],
    "tue": [["09:00", "18:00"]],
    "wed": [["09:00", "18:00"]],
    "thu": [["09:00", "18:00"]],
    "fri": [["09:00", "18:00"]],
}

# Index == `date.weekday()` (Monday == 0 ... Sunday == 6).
DAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
