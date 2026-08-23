"""Analytics-rollup cron job (stage 7 — analytics, PR3).

`rollup_daily_metrics` runs nightly at 00:30 UTC (see
`app.workers.settings.WorkerSettings.cron_jobs`) and recomputes the last
`LOOKBACK_DAYS` COMPLETED org-local calendar days for EVERY organization —
never the current (still in-progress) org-local day (design D3).

Like `app.workers.sla.monitor_sla`, organizations are read via a direct
`OrganizationRepository(session).list()` call — there is no single
`OrgScope` this cross-tenant background job could run its read under (see
`app.repositories.organization_repository`'s module docstring: it is the
one place in the codebase allowed to query organizations unscoped). Each
organization's `(family, local_day)` unit of work runs inside its own
SAVEPOINT (`session.begin_nested()`) wrapped in try/except (design D2): if
one metric family's query or upsert raises for one org/day, only that
family's partial work rolls back, the exception is logged, and the pass
continues with the next family/day/org — one family's failure must never
prevent the other families (or other orgs) from computing and committing.

Idempotency across runs relies entirely on
`analytics_service.upsert_daily_metrics`'s `ON CONFLICT (organization_id,
metric_date, metric_key) DO UPDATE` (D6) — this job carries no "already
processed" state of its own, exactly like `monitor_sla`'s reliance on
`record_once`'s partial unique index.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.models.organization import Organization
from app.repositories.organization_repository import OrganizationRepository
from app.services import analytics_service

logger = logging.getLogger(__name__)

# Recompute the last 2 COMPLETED org-local calendar days per run — never
# today, which is not yet complete in the org's own timezone. A nightly
# 00:30 UTC run comfortably covers every org's local "yesterday" even for
# timezones far ahead of UTC, and re-covering the day before that absorbs
# a missed/late run without any gap.
LOOKBACK_DAYS = 2

# Referenced by ATTRIBUTE NAME, looked up on `analytics_service` at CALL
# TIME (`getattr`) inside `_rollup_family` — never bound to a direct
# function-object reference here at import time. Binding directly would
# capture the original function object in this module's namespace, and a
# test's `monkeypatch.setattr(analytics_service, "tickets_family_metrics",
# fake)` (module-attribute-level patching) would then silently have no
# effect on THIS worker's calls, since the module-attribute lookup would
# never happen again.
_FAMILY_FUNCTION_NAMES = (
    "tickets_family_metrics",
    "sla_family_metrics",
    "chat_family_metrics",
)


def _now() -> datetime:
    """Thin, patchable wrapper around `datetime.now(UTC)` — tests monkeypatch
    this single seam to pin "now" instead of freezing global time."""
    return datetime.now(UTC)


async def rollup_daily_metrics(ctx: dict[str, Any]) -> None:
    """Recompute and upsert `LOOKBACK_DAYS` completed org-local days of
    daily metrics for every organization.

    Opens its own session via `ctx["session_factory"]` (the same
    worker-owned-session pattern as `app.workers.sla.monitor_sla`) and
    commits once at the end of the pass — per-family work is isolated via
    SAVEPOINTs, not by delaying the outer commit.
    """
    session_factory = ctx["session_factory"]
    now = _now()

    async with session_factory() as session:
        organizations = await OrganizationRepository(session).list()

        for organization in organizations:
            for local_day in _completed_local_days(organization, now):
                for family_name in _FAMILY_FUNCTION_NAMES:
                    await _rollup_family(session, organization, local_day, family_name)

        await session.commit()


def _completed_local_days(organization: Organization, now: datetime) -> list[Any]:
    """The last `LOOKBACK_DAYS` COMPLETED calendar days in `organization`'s
    own timezone, most-recent first (yesterday, day-before-yesterday, ...).
    "Today" (offset 0) is deliberately excluded — it is still in progress."""
    local_today = now.astimezone(ZoneInfo(organization.timezone)).date()
    return [local_today - timedelta(days=offset) for offset in range(1, LOOKBACK_DAYS + 1)]


async def _rollup_family(session, organization: Organization, local_day, family_name: str) -> None:
    """Compute one metric family (`family_name` — an
    `app.services.analytics_service` attribute name, looked up at call time
    so tests can monkeypatch it) for one org/day inside its own SAVEPOINT
    and upsert its rows. Any exception (query error, constraint violation
    on upsert, ...) is caught, logged, and rolls back ONLY this family's
    nested transaction — the outer pass continues with the next family."""
    family_fn = getattr(analytics_service, family_name)
    try:
        async with session.begin_nested():
            metrics = await family_fn(session, organization, local_day)
            rows = [
                {
                    "organization_id": organization.id,
                    "metric_date": local_day,
                    "metric_key": metric_key,
                    "metric_value": metric_value,
                }
                for metric_key, metric_value in metrics
            ]
            await analytics_service.upsert_daily_metrics(session, rows)
    except Exception:
        logger.exception(
            "Analytics rollup failed computing %s for org %s, day %s; "
            "continuing with the remaining families/days/orgs",
            family_name,
            organization.id,
            local_day,
        )
