"""SLA due-at compute core (stage 6 — queues + SLA, PR3).

`compute_sla_due_at`/`parse_business_hours` are deliberately pure — no
DB/session imports — so a later PR's `OrganizationUpdate` schema validator
can call `parse_business_hours` directly without an import cycle
(schemas -> services -> ... -> schemas). `resolve_sla_due_at` is the only
async/session-touching function here: it loads an org's `timezone`/
`business_hours` via `OrganizationRepository` and delegates to
`compute_sla_due_at`.

NOT wired into `ticket_service.py`/`classification.py` yet — that is a
later PR's job (writer rewiring). This module only proves the compute core
in isolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.sla_defaults import DAY_KEYS
from app.repositories.organization_repository import OrganizationRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.urgency_level import UrgencyLevel

# Bounds the business-hours walk so a malformed/adversarial config (e.g.
# every day closed) can never infinite-loop. Should be unreachable in
# practice — `parse_business_hours` rejects an all-closed week before it
# reaches the DB — this is a structural safety net, not an expected path.
MAX_WALK_DAYS = 366


class BusinessHoursExhaustedError(Exception):
    """The business-hours walk exceeded `MAX_WALK_DAYS` without landing on
    a due-at instant. See `MAX_WALK_DAYS` above for why this is unreachable
    in practice."""


class InvalidBusinessHoursError(ValueError):
    """A `business_hours` payload is structurally invalid — see
    `parse_business_hours` for every rejected shape."""


def compute_sla_due_at(
    *,
    from_timestamp: datetime,
    sla_hours: int,
    respects_business_hours: bool,
    timezone_name: str,
    business_hours: Mapping[str, Any],
) -> datetime:
    """Compute the tz-aware UTC instant an SLA of `sla_hours` is due,
    starting from `from_timestamp` (any tz — converted as needed).

    If `respects_business_hours` is `False`, the clock never pauses: the
    result is `from_timestamp + sla_hours`, converted to UTC.

    Otherwise, the clock only accrues during the org's weekly
    `business_hours` windows (in `timezone_name`), walking day by day,
    window by window, until `sla_hours` of REAL elapsed time (not nominal
    wall-clock duration) has accrued. Each window's `available` capacity
    converts BOTH ends to UTC before subtracting — a nominal 9h window
    measures as 8 real hours on a DST spring-forward day (one wall-clock
    hour skipped) or 10 on a fall-back day (one hour repeats), instead of
    mismeasuring via naive local wall-clock subtraction.
    """
    if not respects_business_hours:
        return from_timestamp.astimezone(UTC) + timedelta(hours=sla_hours)

    tz = ZoneInfo(timezone_name)
    cursor = from_timestamp.astimezone(tz)
    remaining = timedelta(hours=sla_hours)

    for _ in range(MAX_WALK_DAYS):
        day_key = DAY_KEYS[cursor.weekday()]
        windows = business_hours.get(day_key) or []

        for start, end in windows:
            window_start = datetime.combine(
                cursor.date(), time.fromisoformat(start), tzinfo=tz
            )
            window_end = datetime.combine(
                cursor.date(), time.fromisoformat(end), tzinfo=tz
            )

            if cursor >= window_end:
                continue

            effective_start = max(cursor, window_start)
            available = window_end.astimezone(UTC) - effective_start.astimezone(UTC)

            if remaining <= available:
                return effective_start.astimezone(UTC) + remaining

            remaining -= available
            cursor = window_end

        # Advance to next local midnight — NOT `cursor + timedelta(hours=24)`,
        # which would drift across a DST boundary. Combining fresh at local
        # midnight always lands on a real wall-clock midnight.
        cursor = datetime.combine(
            cursor.date() + timedelta(days=1), time.min, tzinfo=tz
        )

    raise BusinessHoursExhaustedError(
        f"business-hours walk exceeded {MAX_WALK_DAYS} days without landing "
        "on a due-at instant; check the organization's business_hours config"
    )


def parse_business_hours(raw: Mapping[str, Any]) -> None:
    """Validate a `business_hours` payload, raising
    `InvalidBusinessHoursError` for: an unknown day key (not in
    `app.core.sla_defaults.DAY_KEYS`); a pair that isn't exactly
    `[start, end]` strings parseable by `time.fromisoformat`;
    `start >= end` within a pair; overlapping intervals in the same day;
    or zero open intervals across the whole week (would otherwise exhaust
    `MAX_WALK_DAYS` in `compute_sla_due_at`).

    Reused by a later PR's `OrganizationUpdate` schema validator — kept
    here (not `app.core.sla_defaults`) since it raises a service-level
    error type and `sla_defaults` stays pure data.
    """
    has_any_open_interval = False

    for day_key, pairs in raw.items():
        if day_key not in DAY_KEYS:
            raise InvalidBusinessHoursError(f"unknown business_hours day key: {day_key!r}")

        intervals: list[tuple[time, time]] = []
        for pair in pairs:
            if not (isinstance(pair, list | tuple) and len(pair) == 2):
                raise InvalidBusinessHoursError(
                    f"business_hours[{day_key!r}] entry must be exactly "
                    f"[start, end]: {pair!r}"
                )
            start, end = pair
            try:
                start_time = time.fromisoformat(start)
                end_time = time.fromisoformat(end)
            except (TypeError, ValueError) as exc:
                raise InvalidBusinessHoursError(
                    f"business_hours[{day_key!r}] entry has an unparseable "
                    f"time: {pair!r}"
                ) from exc

            if start_time >= end_time:
                raise InvalidBusinessHoursError(
                    f"business_hours[{day_key!r}] entry has start >= end: {pair!r}"
                )

            intervals.append((start_time, end_time))
            has_any_open_interval = True

        for i, (a_start, a_end) in enumerate(intervals):
            for b_start, b_end in intervals[i + 1 :]:
                if a_start < b_end and b_start < a_end:
                    raise InvalidBusinessHoursError(
                        f"business_hours[{day_key!r}] has overlapping intervals"
                    )

    if not has_any_open_interval:
        raise InvalidBusinessHoursError(
            "business_hours has zero open intervals across the entire week"
        )


async def resolve_sla_due_at(
    session: AsyncSession,
    *,
    organization_id: UUID,
    urgency: UrgencyLevel,
    from_timestamp: datetime,
) -> datetime:
    """Load `organization_id`'s `timezone`/`business_hours` and delegate to
    `compute_sla_due_at` with `urgency`'s `sla_hours`/
    `respects_business_hours`. Raises `NotFoundError` if the org does not
    exist (via `OrganizationRepository.get_or_404`). NOT wired into any
    caller yet — see this module's docstring.
    """
    organization = await OrganizationRepository(session).get_or_404(organization_id)
    return compute_sla_due_at(
        from_timestamp=from_timestamp,
        sla_hours=urgency.sla_hours,
        respects_business_hours=urgency.respects_business_hours,
        timezone_name=organization.timezone,
        business_hours=organization.business_hours,
    )
