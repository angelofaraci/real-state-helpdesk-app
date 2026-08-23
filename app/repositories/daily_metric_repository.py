"""`DailyMetricRepository` — direct-scoped (`DailyMetric` owns its own
`organization_id` column, so `scope_path` stays empty), mirroring
`SlaEventRepository`.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Select

from app.models.daily_metric import DailyMetric
from app.repositories.base import ScopedRepository


class DailyMetricRepository(ScopedRepository[DailyMetric]):
    model = DailyMetric

    def series(
        self, *, metric_key: str, start_date: date, end_date: date
    ) -> Select[tuple[DailyMetric]]:
        """A scoped, date-ordered `SELECT` of `(organization, metric_key)`
        rows within `[start_date, end_date]` (inclusive both ends) — backs
        `GET /api/v1/analytics/trends` (stage 7 — analytics, PR4).
        `metric_key` vocabulary validation happens at the API layer
        (`DailyMetric.METRIC_KEYS`), not here."""
        return (
            self.select()
            .where(
                DailyMetric.metric_key == metric_key,
                DailyMetric.metric_date >= start_date,
                DailyMetric.metric_date <= end_date,
            )
            .order_by(DailyMetric.metric_date)
        )
