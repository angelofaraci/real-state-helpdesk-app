"""Unit tests for `app.services.sla` (stage 6 — queues + SLA, PR3).
`compute_sla_due_at`/`parse_business_hours` are pure functions, tested
directly per `tests/unit/test_taxonomy_defaults.py`'s style.
`resolve_sla_due_at`'s test mocks the session like
`tests/unit/test_organization_repository.py` does.

DST tests use `Europe/Madrid`'s 2026 transitions with a `["00:00","09:00"]`
window — NOT the org default `09:00-18:00`, which sits entirely after the
02:00/03:00 transition instant and would not exercise DST math at all (both
endpoints would share one UTC offset). The window must SPAN the transition.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.core.sla_defaults import DEFAULT_BUSINESS_HOURS, DEFAULT_TIMEZONE
from app.models.organization import Organization
from app.models.urgency_level import UrgencyLevel
from app.services.sla import (
    MAX_WALK_DAYS,
    BusinessHoursExhaustedError,
    InvalidBusinessHoursError,
    compute_sla_due_at,
    parse_business_hours,
    resolve_sla_due_at,
)

MADRID = "Europe/Madrid"
TZ = ZoneInfo(DEFAULT_TIMEZONE)


def _local(y, m, d, h, mi=0, tz=TZ) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=tz)


# ---------------------------------------------------------------------------
# compute_sla_due_at
# ---------------------------------------------------------------------------


def test_continuous_when_respects_business_hours_false() -> None:
    """No business-hours involvement: straight `from_timestamp + sla_hours`,
    even across a weekend."""
    due_at = compute_sla_due_at(
        from_timestamp=datetime(2026, 1, 2, 20, 0, tzinfo=UTC),  # Friday
        sla_hours=48,
        respects_business_hours=False,
        timezone_name=DEFAULT_TIMEZONE,
        business_hours=DEFAULT_BUSINESS_HOURS,
    )
    assert due_at == datetime(2026, 1, 4, 20, 0, tzinfo=UTC)  # Sunday


@pytest.mark.parametrize(
    ("from_local", "sla_hours", "expected_local"),
    [
        (_local(2026, 1, 5, 6), 2, _local(2026, 1, 5, 11)),  # Mon before open
        (_local(2026, 1, 5, 20), 1, _local(2026, 1, 6, 10)),  # Mon after close -> Tue
    ],
    ids=["starts_before_opening", "starts_after_closing_rolls_to_next_day"],
)
def test_business_hours_walk_starts_outside_window(
    from_local: datetime, sla_hours: int, expected_local: datetime
) -> None:
    due_at = compute_sla_due_at(
        from_timestamp=from_local,
        sla_hours=sla_hours,
        respects_business_hours=True,
        timezone_name=DEFAULT_TIMEZONE,
        business_hours=DEFAULT_BUSINESS_HOURS,
    )
    assert due_at == expected_local.astimezone(UTC)


def test_business_hours_walk_skips_weekend() -> None:
    """Friday 17:00 + 48h (Medium-urgency-shaped) skips Sat/Sun and lands
    the following Monday. Trace: Fri 17-18 (1h), Mon-Fri 09-18 (9h/day x5 =
    45h) exhausts 46h, leaving 2h -> next Mon 09:00 + 2h."""
    due_at = compute_sla_due_at(
        from_timestamp=_local(2026, 1, 2, 17),  # Friday
        sla_hours=48,
        respects_business_hours=True,
        timezone_name=DEFAULT_TIMEZONE,
        business_hours=DEFAULT_BUSINESS_HOURS,
    )
    assert due_at == _local(2026, 1, 12, 11).astimezone(UTC)  # Monday 11:00
    assert due_at.astimezone(TZ).weekday() == 0  # proves the weekend was skipped


def test_landing_exactly_on_window_boundary() -> None:
    """`remaining == available`: lands precisely at `window_end`, no overflow."""
    due_at = compute_sla_due_at(
        from_timestamp=_local(2026, 1, 5, 9),  # Monday open
        sla_hours=9,  # exactly the whole window
        respects_business_hours=True,
        timezone_name=DEFAULT_TIMEZONE,
        business_hours=DEFAULT_BUSINESS_HOURS,
    )
    assert due_at == _local(2026, 1, 5, 18).astimezone(UTC)


def test_business_hours_walk_dst_spring_forward() -> None:
    """`Europe/Madrid` 2026-03-29 (02:00->03:00 local, one hour skipped). A
    nominal 9h window (`00:00`-`09:00`) spanning the jump has only 8 REAL
    hours of capacity: `window_start` 00:00 local is pre-transition
    (UTC+1 = 2026-03-28T23:00Z); `window_end` 09:00 local is
    post-transition (UTC+2 = 2026-03-29T07:00Z) -> 8h real span."""
    business_hours = {"sun": [["00:00", "09:00"]]}
    from_timestamp = _local(2026, 3, 29, 0, tz=ZoneInfo(MADRID))

    due_at_full = compute_sla_due_at(
        from_timestamp=from_timestamp,
        sla_hours=8,  # real capacity of the window
        respects_business_hours=True,
        timezone_name=MADRID,
        business_hours=business_hours,
    )
    assert due_at_full == datetime(2026, 3, 29, 7, 0, tzinfo=UTC)
    assert due_at_full.astimezone(ZoneInfo(MADRID)).time().hour == 9  # lands at close

    # Triangulation: 1h short of the REAL capacity must land 1h earlier,
    # proving the 8h figure is real elapsed time, not a nominal coincidence.
    due_at_short = compute_sla_due_at(
        from_timestamp=from_timestamp,
        sla_hours=7,
        respects_business_hours=True,
        timezone_name=MADRID,
        business_hours=business_hours,
    )
    assert due_at_short == datetime(2026, 3, 29, 6, 0, tzinfo=UTC)


def test_business_hours_walk_dst_fall_back() -> None:
    """`Europe/Madrid` 2026-10-25 (03:00->02:00 local, one hour repeats).
    The same nominal 9h window (`00:00`-`09:00`) now has 10 REAL hours of
    capacity."""
    business_hours = {"sun": [["00:00", "09:00"]]}
    from_timestamp = _local(2026, 10, 25, 0, tz=ZoneInfo(MADRID))

    due_at_full = compute_sla_due_at(
        from_timestamp=from_timestamp,
        sla_hours=10,  # real capacity of the window
        respects_business_hours=True,
        timezone_name=MADRID,
        business_hours=business_hours,
    )
    assert due_at_full == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)
    assert due_at_full.astimezone(ZoneInfo(MADRID)).time().hour == 9  # lands at close

    # Triangulation: the NOMINAL 9h window size must land strictly before
    # window_end — the extra repeated hour is genuinely still available.
    due_at_nominal = compute_sla_due_at(
        from_timestamp=from_timestamp,
        sla_hours=9,
        respects_business_hours=True,
        timezone_name=MADRID,
        business_hours=business_hours,
    )
    assert due_at_nominal < due_at_full


def test_all_closed_business_hours_raises_bounded_error() -> None:
    """An all-closed weekly schedule raises within `MAX_WALK_DAYS` (366)
    iterations — never hangs."""
    assert MAX_WALK_DAYS == 366
    with pytest.raises(BusinessHoursExhaustedError):
        compute_sla_due_at(
            from_timestamp=_local(2026, 1, 5, 9),
            sla_hours=1,
            respects_business_hours=True,
            timezone_name=DEFAULT_TIMEZONE,
            business_hours={},
        )


# ---------------------------------------------------------------------------
# parse_business_hours
# ---------------------------------------------------------------------------


def test_accepts_valid_schedule() -> None:
    parse_business_hours(DEFAULT_BUSINESS_HOURS)  # must not raise
    parse_business_hours({"mon": [["09:00", "12:00"], ["13:00", "18:00"]]})


def test_rejects_unknown_day_key() -> None:
    with pytest.raises(InvalidBusinessHoursError):
        parse_business_hours({"monday": [["09:00", "18:00"]]})


@pytest.mark.parametrize(
    "pair",
    [["09:00"], ["not-a-time", "18:00"]],
    ids=["not_exactly_a_start_end_pair", "unparseable_time"],
)
def test_rejects_malformed_pair(pair: list[str]) -> None:
    with pytest.raises(InvalidBusinessHoursError):
        parse_business_hours({"mon": [pair]})


@pytest.mark.parametrize(
    "pair", [["18:00", "09:00"], ["09:00", "09:00"]], ids=["start_gt_end", "start_eq_end"]
)
def test_rejects_start_gte_end(pair: list[str]) -> None:
    with pytest.raises(InvalidBusinessHoursError):
        parse_business_hours({"mon": [pair]})


def test_rejects_overlapping_intervals() -> None:
    with pytest.raises(InvalidBusinessHoursError):
        parse_business_hours({"mon": [["09:00", "13:00"], ["12:00", "18:00"]]})


@pytest.mark.parametrize(
    "raw", [{}, {"mon": [], "tue": []}], ids=["no_days_present", "days_present_but_empty"]
)
def test_rejects_all_empty_week(raw: dict) -> None:
    with pytest.raises(InvalidBusinessHoursError):
        parse_business_hours(raw)


# ---------------------------------------------------------------------------
# resolve_sla_due_at — async shell
# ---------------------------------------------------------------------------


def _org(**overrides) -> Organization:
    defaults = dict(
        id=uuid.uuid4(),
        name="Acme Corp",
        chat_widget_key="widget-key",
        timezone=DEFAULT_TIMEZONE,
        business_hours=DEFAULT_BUSINESS_HOURS,
    )
    defaults.update(overrides)
    return Organization(**defaults)


def _urgency(**overrides) -> UrgencyLevel:
    defaults = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Medium",
        sla_hours=2,
        respects_business_hours=True,
    )
    defaults.update(overrides)
    return UrgencyLevel(**defaults)


def _session_returning(scalar_result) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session.execute.return_value = execute_result
    return session


@pytest.mark.asyncio
async def test_resolve_sla_due_at_loads_org_config_and_delegates() -> None:
    org = _org()
    urgency = _urgency(organization_id=org.id)
    from_timestamp = _local(2026, 1, 5, 9)

    due_at = await resolve_sla_due_at(
        _session_returning(org),
        organization_id=org.id,
        urgency=urgency,
        from_timestamp=from_timestamp,
    )

    direct = compute_sla_due_at(
        from_timestamp=from_timestamp,
        sla_hours=urgency.sla_hours,
        respects_business_hours=urgency.respects_business_hours,
        timezone_name=org.timezone,
        business_hours=org.business_hours,
    )
    assert due_at == direct == _local(2026, 1, 5, 11).astimezone(UTC)


@pytest.mark.asyncio
async def test_resolve_sla_due_at_uses_the_requested_organizations_config() -> None:
    """Triangulation: a DIFFERENT org's tz/business_hours changes the
    result, proving the loaded row's config is really threaded through —
    not a hardcoded default."""
    org = _org(timezone=MADRID, business_hours={"mon": [["10:00", "19:00"]]})
    urgency = _urgency(organization_id=org.id, sla_hours=1)
    from_timestamp = _local(2026, 1, 5, 8, tz=ZoneInfo(MADRID))

    due_at = await resolve_sla_due_at(
        _session_returning(org),
        organization_id=org.id,
        urgency=urgency,
        from_timestamp=from_timestamp,
    )

    assert due_at == _local(2026, 1, 5, 11, tz=ZoneInfo(MADRID)).astimezone(UTC)
