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

# A ticket is flagged "at risk of breach" once this fraction of its total
# SLA window (`created_at` -> `sla_due_at`) has elapsed. RESOLVED decision
# for stage 6 PR6: warn at 80% elapsed, breach at 100% elapsed
# (`ticket.sla_due_at` itself). Promoted from `app.workers.sla`'s
# previously-private `_SLA_WARNING_ELAPSED_FRACTION` (stage 7 — analytics,
# PR2) so other SLA-adjacent code (e.g. a future analytics rollup) can
# share the exact same threshold without importing the worker module.
SLA_WARNING_ELAPSED_FRACTION = 0.8

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
