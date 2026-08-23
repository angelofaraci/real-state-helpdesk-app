"""Unit tests for `DailyMetricRepository`, the direct-scoped repository for
`DailyMetric` (owns its own `organization_id` column). Mirrors
`test_sla_event_repository.py`'s pattern.
"""

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

from app.core.scope import OrgScope
from app.models.enums import UserRole
from app.repositories.base import ScopedRepository
from app.repositories.daily_metric_repository import DailyMetricRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_daily_metric_repository_extends_scoped_repository() -> None:
    assert issubclass(DailyMetricRepository, ScopedRepository)


def test_daily_metric_repository_is_direct_scoped_no_join_path() -> None:
    assert DailyMetricRepository.scope_path == ()


def test_add_forces_scope_organization_id_discarding_caller_value() -> None:
    scope = _scope()
    session = MagicMock()
    repo = DailyMetricRepository(session, scope)
    wrong_org_id = uuid.uuid4()

    metric = repo.add(
        metric_date=date(2026, 8, 22),
        metric_key="tickets_created",
        metric_value=Decimal("3.0000"),
        organization_id=wrong_org_id,
    )

    assert metric.organization_id == scope.organization_id
    assert metric.organization_id != wrong_org_id
    assert metric.metric_key == "tickets_created"
    session.add.assert_called_once_with(metric)


def test_select_is_scoped_by_organization_id() -> None:
    scope = _scope()
    repo = DailyMetricRepository(MagicMock(), scope)

    sql = _compiled(repo.select())

    assert "daily_metrics" in sql
    assert "daily_metrics.organization_id" in sql
    assert scope.organization_id.hex in sql
