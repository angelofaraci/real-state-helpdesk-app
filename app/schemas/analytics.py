"""Pydantic response schemas for the `/api/v1/analytics/*` endpoints
(stage 7 — analytics, PR4).

All five routes are gated by `require_org_admin` (see
`app.api.v1.analytics`) — analytics is an admin-only concern in this
stage, mirroring the precedent `app.api.v1.knowledge_base` set for
admin-only write access, generalized here to the whole surface (reads
included) because these routes run LIVE cross-model aggregate queries
(`tickets` joined to `messages`/`classifications`/`chat_sessions`)
directly against `Ticket.organization_id`, not through
`TicketRepository.select()`'s role-fused visibility narrowing (see that
module's docstring, Rule C). That narrowing only matters for
tenant/owner/agent roles; `ADMIN` already sees every ticket in its
organization unrestricted, so a direct org-scoped join is exactly as safe
as going through the repository for that one role — but it would be
UNSAFE to expose to tenant/owner/agent, hence the admin-only gate on
every route here, not just the ones that write.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import TicketChannel, TicketStatus

# `GET /analytics/trends`'s `end_date - start_date` span cap, in days —
# protects the query from an unbounded full-table date-range scan.
TREND_MAX_SPAN_DAYS = 366

# Default lookback window (in days) applied to `/trends` when both
# `start_date`/`end_date` are omitted, and to `/overview`'s 30-day channel
# breakdown.
TREND_DEFAULT_LOOKBACK_DAYS = 30

# The classifier sample-window size and needs-review threshold live in
# `app.services.analytics_service` (`CLASSIFIER_SAMPLE_WINDOW`,
# `CLASSIFIER_NEEDS_REVIEW_THRESHOLD`) — they shape the query/flagging
# logic, not the response shape, so this module does not duplicate them.


# ---------------------------------------------------------------------------
# GET /analytics/overview
# ---------------------------------------------------------------------------


class StatusCount(BaseModel):
    status: TicketStatus
    count: int


class UrgencyCount(BaseModel):
    urgency_id: UUID | None
    urgency_name: str | None
    count: int


class ChannelCount(BaseModel):
    channel: TicketChannel
    count: int


class SlaWindowCounts(BaseModel):
    """Live counts of currently-open tickets in each SLA window, per
    `SLA_WARNING_ELAPSED_FRACTION` (`app.core.sla_defaults`):
    `warning` = elapsed fraction of the SLA window is >= the threshold but
    the due date has not yet passed; `breached` = the due date has
    already passed. A ticket with no `sla_due_at` (not yet classified) is
    in neither bucket."""

    warning: int
    breached: int


class OverviewResponse(BaseModel):
    open_by_status: list[StatusCount]
    open_by_urgency: list[UrgencyCount]
    sla_window: SlaWindowCounts
    channel_breakdown_30d: list[ChannelCount]


# ---------------------------------------------------------------------------
# GET /analytics/trends
# ---------------------------------------------------------------------------


class TrendPoint(BaseModel):
    metric_date: date
    metric_value: Decimal


class TrendResponse(BaseModel):
    metric_key: str
    start_date: date
    end_date: date
    points: list[TrendPoint]


# ---------------------------------------------------------------------------
# GET /analytics/classifier
# ---------------------------------------------------------------------------


class ClassifierCategoryReview(BaseModel):
    category_id: UUID
    category_name: str
    sample_size: int
    correction_rate: Decimal
    needs_review: bool


class ClassifierResponse(BaseModel):
    categories: list[ClassifierCategoryReview]
    # Trended from `daily_metrics`, written nightly by the rollup worker's
    # `classifier_family_metrics` (PR6). May still be empty for an org/day
    # with zero classifications that day (both metrics are omitted, not
    # written as `0` — see `analytics_service.classifier_family_metrics`),
    # or before the nightly rollup has run/accumulated history for a given
    # org — neither case is a bug in this endpoint.
    accuracy_trend: list[TrendPoint]
    llm_fallback_rate_trend: list[TrendPoint]


# ---------------------------------------------------------------------------
# GET /analytics/rag
# ---------------------------------------------------------------------------


class RagCategoryBreakdown(BaseModel):
    category_id: UUID
    category_name: str
    suggestions_generated: int
    suggestions_used: int


class RagResponse(BaseModel):
    suggestions_generated: int
    suggestions_used: int
    usage_rate: Decimal | None
    category_breakdown: list[RagCategoryBreakdown]


# ---------------------------------------------------------------------------
# GET /analytics/chatbot
# ---------------------------------------------------------------------------


class ToolUsageCount(BaseModel):
    tool_name: str
    count: int


class ChatbotResponse(BaseModel):
    total_sessions: int
    escalated_sessions: int
    escalation_rate: Decimal | None
    avg_messages_per_session: Decimal | None
    tool_usage: list[ToolUsageCount]
