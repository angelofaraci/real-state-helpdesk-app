"""`DailyMetricRepository` — direct-scoped (`DailyMetric` owns its own
`organization_id` column, so `scope_path` stays empty), mirroring
`SlaEventRepository`.
"""

from __future__ import annotations

from app.models.daily_metric import DailyMetric
from app.repositories.base import ScopedRepository


class DailyMetricRepository(ScopedRepository[DailyMetric]):
    model = DailyMetric
