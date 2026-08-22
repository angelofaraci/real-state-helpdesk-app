"""Unit tests for `app.schemas.urgency_level` — stage 6 (queues + SLA, PR4)
addition: `respects_business_hours` must round-trip through
`UrgencyLevelCreate`/`UrgencyLevelUpdate`/`UrgencyLevelResponse`, mirroring
how `sla_hours` is already declared on each of those three schemas.
"""

from datetime import UTC, datetime
from uuid import uuid4

from app.models.urgency_level import UrgencyLevel
from app.schemas.urgency_level import (
    UrgencyLevelCreate,
    UrgencyLevelResponse,
    UrgencyLevelUpdate,
)


def test_urgency_level_create_accepts_respects_business_hours() -> None:
    create = UrgencyLevelCreate(name="Critical", sla_hours=2, respects_business_hours=False)

    assert create.respects_business_hours is False


def test_urgency_level_create_defaults_respects_business_hours_to_true() -> None:
    """Matches the DB column's `server_default=true` — an omitted field on
    a client-supplied create payload should not silently become `False`."""
    create = UrgencyLevelCreate(name="Low", sla_hours=120)

    assert create.respects_business_hours is True


def test_urgency_level_update_accepts_respects_business_hours() -> None:
    update = UrgencyLevelUpdate(respects_business_hours=True)

    assert update.respects_business_hours is True


def test_urgency_level_update_defaults_respects_business_hours_to_none() -> None:
    """`None` on `Update` means "not being changed" (same convention as
    every other field on this PATCH schema), NOT "set to `False`"."""
    update = UrgencyLevelUpdate()

    assert update.respects_business_hours is None


def test_urgency_level_response_round_trips_respects_business_hours_from_the_orm_row() -> None:
    urgency = UrgencyLevel(
        id=uuid4(),
        organization_id=uuid4(),
        name="Medium",
        sla_hours=48,
        sort_order=2,
        active=True,
        respects_business_hours=True,
    )
    urgency.created_at = datetime.now(UTC)

    response = UrgencyLevelResponse.model_validate(urgency)

    assert response.respects_business_hours is True
