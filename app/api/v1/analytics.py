"""Analytics read endpoints (stage 7 — analytics, PR4).

Every route here is gated by `require_org_admin` — analytics is an
admin-only concern in this stage. This is a STRICTER gate than most other
read endpoints in this codebase (e.g. `app.api.v1.knowledge_base` allows
`require_org_staff` for reads), and it is deliberate: `/overview`,
`/classifier`, and `/rag` all run LIVE cross-model aggregate queries
(`app.services.analytics_service`) directly against `Ticket
.organization_id`, bypassing `TicketRepository.select()`'s role-fused
visibility narrowing (see that module's docstring, Rule C — a
tenant/owner/agent only sees a role-restricted subset of tickets, while
`ADMIN` sees every ticket in the organization unrestricted). A direct
org-scoped join is exactly as safe as going through the repository for
the `ADMIN` role specifically — but would silently leak cross-ticket
aggregate data to a tenant/owner/agent if this gate were ever relaxed to
`require_org_staff` or `require_org_member`. `/trends` (`daily_metrics`,
which has no role-visibility concept at all) and `/chatbot` (`chat_
sessions`/`chat_messages`, likewise org-only) do not strictly need this
strength of gate on their own, but are held to the same admin-only bar
for one uniform, easy-to-audit rule across the whole analytics surface
(task 4.13/4.14's RBAC parametrization tests this uniformity directly).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_org_admin
from app.core.scope import OrgScope
from app.core.session import get_session
from app.models.daily_metric import METRIC_KEYS
from app.models.organization import Organization
from app.repositories.daily_metric_repository import DailyMetricRepository
from app.repositories.organization_repository import OrganizationRepository
from app.schemas.analytics import (
    TREND_MAX_SPAN_DAYS,
    ChannelCount,
    ChatbotResponse,
    ClassifierCategoryReview,
    ClassifierResponse,
    OverviewResponse,
    RagCategoryBreakdown,
    RagResponse,
    SlaWindowCounts,
    StatusCount,
    ToolUsageCount,
    TrendPoint,
    TrendResponse,
    UrgencyCount,
)
from app.services import analytics_service

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

_OVERVIEW_CHANNEL_LOOKBACK_DAYS = 30
_TRENDS_DEFAULT_LOOKBACK_DAYS = 30


async def _load_organization(session: AsyncSession, scope: OrgScope) -> Organization:
    """Every route here needs the full `Organization` row (not just its
    id) — `analytics_service`'s query functions take an `Organization`
    instance, mirroring PR3's rollup-worker functions."""
    return await OrganizationRepository(session).get_or_404(scope.organization_id)


@router.get("/overview", response_model=OverviewResponse)
async def get_overview(
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> OverviewResponse:
    """Live snapshot (NOT read from `daily_metrics`) of open tickets by
    status/urgency, tickets in the SLA warning/breached window, and a
    rolling 30-day channel breakdown."""
    organization = await _load_organization(session, scope)
    now = datetime.now(UTC)
    since = now - timedelta(days=_OVERVIEW_CHANNEL_LOOKBACK_DAYS)

    by_status = await analytics_service.open_ticket_counts_by_status(session, organization)
    by_urgency = await analytics_service.open_ticket_counts_by_urgency(session, organization)
    warning, breached = await analytics_service.sla_window_counts(
        session, organization, now=now
    )
    by_channel = await analytics_service.channel_breakdown_counts(
        session, organization, since=since
    )

    return OverviewResponse(
        open_by_status=[StatusCount(status=s, count=c) for s, c in by_status],
        open_by_urgency=[
            UrgencyCount(urgency_id=uid, urgency_name=name, count=c)
            for uid, name, c in by_urgency
        ],
        sla_window=SlaWindowCounts(warning=warning, breached=breached),
        channel_breakdown_30d=[ChannelCount(channel=ch, count=c) for ch, c in by_channel],
    )


@router.get("/trends", response_model=TrendResponse)
async def get_trends(
    metric_key: str,
    start_date: date | None = None,
    end_date: date | None = None,
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> TrendResponse:
    """A `daily_metrics` time series for one `metric_key`, within
    `[start_date, end_date]` (both default to the last 30 days when
    omitted). `metric_key` must be a known `DailyMetric.METRIC_KEYS`
    value; `end_date < start_date` or a span exceeding
    `TREND_MAX_SPAN_DAYS` (366 days) both fail with 422."""
    if metric_key not in METRIC_KEYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown metric_key: {metric_key!r}",
        )

    resolved_end = end_date or datetime.now(UTC).date()
    resolved_start = start_date or (
        resolved_end - timedelta(days=_TRENDS_DEFAULT_LOOKBACK_DAYS - 1)
    )

    if resolved_end < resolved_start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_date must not be before start_date",
        )
    span_days = (resolved_end - resolved_start).days
    if span_days > TREND_MAX_SPAN_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"date span exceeds the {TREND_MAX_SPAN_DAYS}-day maximum",
        )

    repo = DailyMetricRepository(session, scope)
    stmt = repo.series(metric_key=metric_key, start_date=resolved_start, end_date=resolved_end)
    rows = (await session.execute(stmt)).scalars().all()

    return TrendResponse(
        metric_key=metric_key,
        start_date=resolved_start,
        end_date=resolved_end,
        points=[
            TrendPoint(metric_date=row.metric_date, metric_value=row.metric_value)
            for row in rows
        ],
    )


@router.get("/classifier", response_model=ClassifierResponse)
async def get_classifier(
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> ClassifierResponse:
    """Per-category classifier correction-rate review (live query, most
    recent 200 classifications per category), plus the `classifier_
    accuracy`/`llm_fallback_rate` trend series from `daily_metrics` —
    written nightly by the rollup worker's `classifier_family_metrics`
    (PR6). A series may still come back empty for an org/day with zero
    classifications that day, or before the rollup has accumulated
    history for this org."""
    organization = await _load_organization(session, scope)
    rows = await analytics_service.classifier_review_metrics(session, organization)

    repo = DailyMetricRepository(session, scope)
    today = datetime.now(UTC).date()
    default_start = today - timedelta(days=_TRENDS_DEFAULT_LOOKBACK_DAYS - 1)
    accuracy_rows = (
        await session.execute(
            repo.series(
                metric_key="classifier_accuracy", start_date=default_start, end_date=today
            )
        )
    ).scalars().all()
    fallback_rows = (
        await session.execute(
            repo.series(
                metric_key="llm_fallback_rate", start_date=default_start, end_date=today
            )
        )
    ).scalars().all()

    return ClassifierResponse(
        categories=[
            ClassifierCategoryReview(
                category_id=category_id,
                category_name=category_name,
                sample_size=sample_size,
                correction_rate=correction_rate,
                needs_review=correction_rate
                > analytics_service.CLASSIFIER_NEEDS_REVIEW_THRESHOLD,
            )
            for category_id, category_name, sample_size, correction_rate in rows
        ],
        accuracy_trend=[
            TrendPoint(metric_date=row.metric_date, metric_value=row.metric_value)
            for row in accuracy_rows
        ],
        llm_fallback_rate_trend=[
            TrendPoint(metric_date=row.metric_date, metric_value=row.metric_value)
            for row in fallback_rows
        ],
    )


@router.get("/rag", response_model=RagResponse)
async def get_rag(
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> RagResponse:
    """RAG suggestion usage (source: `messages`, never `chat_messages`)
    and its per-category breakdown."""
    organization = await _load_organization(session, scope)
    generated, used = await analytics_service.rag_suggestion_totals(session, organization)
    breakdown = await analytics_service.rag_category_breakdown(session, organization)

    usage_rate = None if generated == 0 else Decimal(used) / Decimal(generated)

    return RagResponse(
        suggestions_generated=generated,
        suggestions_used=used,
        usage_rate=usage_rate,
        category_breakdown=[
            RagCategoryBreakdown(
                category_id=category_id,
                category_name=category_name,
                suggestions_generated=cat_generated,
                suggestions_used=cat_used,
            )
            for category_id, category_name, cat_generated, cat_used in breakdown
        ],
    )


@router.get("/chatbot", response_model=ChatbotResponse)
async def get_chatbot(
    scope: OrgScope = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session),
) -> ChatbotResponse:
    """Chatbot totals (all-time), escalation rate, average messages per
    session, and tool-usage counts."""
    organization = await _load_organization(session, scope)
    total, escalated = await analytics_service.chatbot_session_totals(session, organization)
    avg_messages = await analytics_service.chatbot_avg_messages_per_session(
        session, organization
    )
    tool_usage = await analytics_service.chatbot_tool_usage_counts(session, organization)

    escalation_rate = None if total == 0 else Decimal(escalated) / Decimal(total)

    return ChatbotResponse(
        total_sessions=total,
        escalated_sessions=escalated,
        escalation_rate=escalation_rate,
        avg_messages_per_session=avg_messages,
        tool_usage=[
            ToolUsageCount(tool_name=name, count=c) for name, c in tool_usage
        ],
    )
