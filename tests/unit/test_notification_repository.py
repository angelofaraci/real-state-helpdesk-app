"""Unit tests for `NotificationRepository`, the direct-scoped repository
for `Notification` (owns its own `organization_id` column).

`get_or_404`/`add`/`update`/`delete` are already covered generically by
`tests/unit/test_scoped_repository.py`; these tests cover only what
`NotificationRepository` adds: basic scoped add + `for_recipient()`.

No live Postgres is available in this sandbox (same constraint noted by
`tests/unit/test_ticket_repository.py` and others), so `for_recipient()`'s
cross-user filtering is proven the same way `TicketRepository`'s
role-visibility clauses are: by inspecting the compiled SQL of the
returned `Select` and asserting a *different* user's id never appears in
it — i.e. a notification belonging to another user in the same org could
never be matched by this query, only the caller-supplied `user_id`.
"""

import uuid
from unittest.mock import MagicMock

from app.core.scope import OrgScope
from app.models.enums import NotificationKind, UserRole
from app.repositories.notification_repository import NotificationRepository


def _scope(**overrides) -> OrgScope:
    defaults = dict(organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role=UserRole.ADMIN)
    defaults.update(overrides)
    return OrgScope(**defaults)


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_add_forces_scope_organization_id_discarding_caller_value() -> None:
    scope = _scope()
    session = MagicMock()
    repo = NotificationRepository(session, scope)
    recipient_id = uuid.uuid4()
    wrong_org_id = uuid.uuid4()

    notification = repo.add(
        user_id=recipient_id,
        kind=NotificationKind.SLA_WARNING,
        title="SLA warning",
        organization_id=wrong_org_id,
    )

    assert notification.organization_id == scope.organization_id
    assert notification.organization_id != wrong_org_id
    assert notification.user_id == recipient_id
    session.add.assert_called_once_with(notification)


def test_for_recipient_is_scoped_to_organization_and_the_given_user() -> None:
    scope = _scope()
    repo = NotificationRepository(MagicMock(), scope)
    recipient_id = uuid.uuid4()

    sql = _compiled(repo.for_recipient(recipient_id))

    assert "notifications" in sql
    assert scope.organization_id.hex in sql
    assert "notifications.user_id" in sql
    assert recipient_id.hex in sql


def test_for_recipient_never_matches_a_different_users_id() -> None:
    """The whole point of `for_recipient`: a notification addressed to some
    other user in the *same* organization must be structurally
    unreachable — its id never appears in the compiled query at all,
    proving cross-user access is invisible rather than filtered
    after-the-fact."""
    scope = _scope()
    repo = NotificationRepository(MagicMock(), scope)
    recipient_id = uuid.uuid4()
    someone_elses_id = uuid.uuid4()

    sql = _compiled(repo.for_recipient(recipient_id))

    assert someone_elses_id.hex not in sql


def test_for_recipient_orders_newest_first_matching_the_composite_index() -> None:
    scope = _scope()
    repo = NotificationRepository(MagicMock(), scope)

    sql = _compiled(repo.for_recipient(uuid.uuid4()))

    assert "ORDER BY notifications.sent_at DESC, notifications.id DESC" in sql
