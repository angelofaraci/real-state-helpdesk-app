"""Integration tests for Stage 2 (ticket classification) real-Postgres-only
behaviors, following the same skip-stub pattern as
`tests/integration/test_auth_flow_pg.py`.

Everything in `tests/unit/` and `tests/api/` mocks the `AsyncSession` /
repository boundary, which is sufficient to prove service and HTTP-layer
control flow (branching logic, status codes, response shapes, and the SQL
strings that SQLAlchemy compiles). It does NOT prove that:

- The `ck_tickets_classified_has_taxonomy` CHECK constraint (added in
  migration `0003_classification_stage2.py`) is actually enforced by
  Postgres itself when a row violates it outside the ORM's own validation
  path (e.g. a raw UPDATE, a buggy call site, or a future code path that
  forgets to set taxonomy before flipping `classification_status`).
- `SELECT ... FOR UPDATE` in `app.workers.classification.classify_ticket`
  actually serializes two concurrent job deliveries for the same
  `ticket_id`, so that only one of them performs the classification write
  and the other observes `classification_status != "pending"` and returns
  as a no-op (arq's at-least-once delivery semantics rely on this).
- `ClassificationRepository.upsert`'s `INSERT ... ON CONFLICT (ticket_id)
  DO UPDATE` actually behaves as an upsert against a real unique
  constraint: first call inserts a new `classifications` row, a second
  call for the same `ticket_id` updates that same row in place (no
  duplicate row, no `IntegrityError`).

No live Postgres is reachable in this sandbox. These tests are
intentionally skipped rather than faking Postgres-specific behavior with
SQLite (SQLite does not enforce CHECK constraints referencing multiple
columns in the same way, does not support `FOR UPDATE` row locking
semantics, and does not have `ON CONFLICT ... DO UPDATE` compiled the same
way as the Postgres dialect). They document the exact scope that remains
unverified against a real database and should be run manually or in CI
against `docker-compose up -d postgres` once a Postgres integration test
harness exists.
"""

import pytest


@pytest.mark.skip(
    reason="requires live PostgreSQL — no integration harness configured yet"
)
async def test_classified_ticket_without_taxonomy_violates_check_constraint() -> None:
    """Placeholder for the real assertion against Postgres:

    1. Insert a ticket row directly (bypassing the ORM-level validation
       path) with `classification_status='classified'` and
       `category_id=None` (or `urgency_id=None` / `sla_due_at=None`).
    2. Assert the INSERT/UPDATE raises an `IntegrityError` /
       `CheckViolationError` from the `ck_tickets_classified_has_taxonomy`
       constraint defined in migration `0003`.
    3. Assert the same statement succeeds once taxonomy fields are all
       non-null, or `classification_status` is `pending`.
    """
    raise NotImplementedError("run against a live Postgres instance")


@pytest.mark.skip(
    reason="requires live PostgreSQL — no integration harness configured yet"
)
async def test_concurrent_classify_ticket_jobs_do_not_double_classify() -> None:
    """Placeholder for the real assertion against Postgres:

    1. Create a `pending` ticket.
    2. Launch two concurrent `classify_ticket(ctx, ticket_id, org_id)` calls
       (e.g. via `asyncio.gather`, each with its own session from
       `session_factory`) against the same `ticket_id`.
    3. Assert the `SELECT ... FOR UPDATE` in `TicketRepository.get_or_404`
       serializes them: the second caller only proceeds once the first has
       committed, observes `classification_status != "pending"`, and
       returns as a no-op.
    4. Assert exactly one `classifications` row exists for the ticket
       afterward, and the classifier was invoked exactly once (not twice).
    """
    raise NotImplementedError("run against a live Postgres instance")


@pytest.mark.skip(
    reason="requires live PostgreSQL — no integration harness configured yet"
)
async def test_classification_repository_upsert_inserts_then_updates_in_place() -> None:
    """Placeholder for the real assertion against Postgres:

    1. Call `ClassificationRepository.upsert(ticket_id, ...)` for a
       `ticket_id` with no existing `classifications` row. Commit. Assert
       exactly one row exists with the given values.
    2. Call `upsert` again for the same `ticket_id` with different
       values (e.g. a re-classification / human-correction follow-up).
       Commit. Assert the row count is still exactly one, and its columns
       now reflect the second call's values (not the first), proving the
       `ON CONFLICT (ticket_id) DO UPDATE` path — not a second inserted
       row and not a raised `IntegrityError` from the unique constraint.
    """
    raise NotImplementedError("run against a live Postgres instance")
