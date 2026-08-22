"""Unit tests for `SlaEventRepository`, the direct-scoped repository for
`SlaEvent` (owns its own `organization_id` column).

`get_or_404`/`add`/`update`/`delete` are already covered generically by
`tests/unit/test_scoped_repository.py`; these tests cover only what
`SlaEventRepository` adds: basic scoped add + `list_for_ticket()`,
mirroring `CategoryRepository`/`UrgencyLevelRepository`'s pattern.
"""

import uuid
from unittest.mock import MagicMock

from app.core.scope import OrgScope
from app.models.enums import SlaEventType, UserRole
from app.repositories.sla_event_repository import SlaEventRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_add_forces_scope_organization_id_discarding_caller_value() -> None:
    scope = _scope()
    session = MagicMock()
    repo = SlaEventRepository(session, scope)
    ticket_id = uuid.uuid4()
    wrong_org_id = uuid.uuid4()

    event = repo.add(
        ticket_id=ticket_id,
        event=SlaEventType.WARNING,
        organization_id=wrong_org_id,
    )

    assert event.organization_id == scope.organization_id
    assert event.organization_id != wrong_org_id
    assert event.ticket_id == ticket_id
    assert event.event == SlaEventType.WARNING
    session.add.assert_called_once_with(event)


def test_list_for_ticket_is_scoped_and_filters_by_ticket_id() -> None:
    scope = _scope()
    repo = SlaEventRepository(MagicMock(), scope)
    ticket_id = uuid.uuid4()

    sql = _compiled(repo.list_for_ticket(ticket_id))

    assert "sla_events" in sql
    assert scope.organization_id.hex in sql
    assert "sla_events.ticket_id" in sql
    assert ticket_id.hex in sql


def test_list_for_ticket_orders_by_occurred_at() -> None:
    scope = _scope()
    repo = SlaEventRepository(MagicMock(), scope)

    sql = _compiled(repo.list_for_ticket(uuid.uuid4()))

    assert "ORDER BY sla_events.occurred_at" in sql
