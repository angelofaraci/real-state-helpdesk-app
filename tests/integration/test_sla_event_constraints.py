"""Integration tests for Stage 6 (queues + SLA) real-Postgres-only
constraint enforcement, following the same skip-stub pattern as
`tests/integration/test_channel_identity_constraints.py` and
`tests/integration/test_classification_constraints.py`.

`tests/unit/test_sla_event_model.py` and `tests/unit/test_notification_model.py`
prove the ORM metadata declares the right shape (columns, FK targets,
enum, indexes, check constraint text) via pure `Base.metadata` inspection.
They do NOT prove that:

- The real `ux_sla_events_ticket_event_once` partial unique index
  (migration 0009) actually rejects a second `(ticket_id, event='warning')`
  row for the same ticket at the database level, while allowing a second
  `event='resolved'` row for that same ticket (the reopen/re-resolve
  case the partial predicate deliberately preserves).
- The real `ck_notifications_title_not_blank` CHECK constraint enforces a
  non-blank title independently of any Python-side validation a later PR
  might add on top.

No live Postgres is reachable in this sandbox. These tests are
intentionally skipped rather than faking Postgres-specific behavior with
SQLite, and should be run manually or in CI against
`docker compose up -d postgres` once a Postgres integration test harness
exists.
"""

import pytest


@pytest.mark.skip(
    reason="Requires a live Postgres instance (see docker-compose.yml); "
    "not reachable in CI (no Postgres service configured). Run manually "
    "with `docker compose up -d db && pytest tests/integration -m ''`."
)
async def test_duplicate_sla_warning_event_for_the_same_ticket_is_rejected() -> None:
    """Inserting a second `SlaEvent(ticket_id=X, event='warning')` row for
    a ticket that already has one must raise `IntegrityError` against the
    real `ux_sla_events_ticket_event_once` partial unique index."""


@pytest.mark.skip(
    reason="Requires a live Postgres instance (see docker-compose.yml); "
    "not reachable in CI (no Postgres service configured). Run manually "
    "with `docker compose up -d db && pytest tests/integration -m ''`."
)
async def test_repeated_resolved_events_for_the_same_ticket_are_allowed() -> None:
    """Two `SlaEvent(ticket_id=X, event='resolved')` rows for the same
    ticket must both insert successfully — `resolved` is deliberately
    excluded from the partial unique index's predicate so a ticket's
    reopen/re-resolve cycle can record it repeatedly."""


@pytest.mark.skip(
    reason="Requires a live Postgres instance (see docker-compose.yml); "
    "not reachable in CI (no Postgres service configured). Run manually "
    "with `docker compose up -d db && pytest tests/integration -m ''`."
)
async def test_blank_notification_title_is_rejected_by_the_check_constraint() -> None:
    """Inserting a `Notification` with a blank/whitespace-only `title`
    must raise `IntegrityError` against the real
    `ck_notifications_title_not_blank` CHECK constraint, independent of
    any Python-side validation."""
