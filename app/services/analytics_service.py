"""Stage 7 — analytics rollup query builders and upsert mechanics (PR3).

`rollup_daily_metrics` (a later module, `app.workers.analytics`) calls the
per-family aggregator functions below (`tickets_family_metrics`,
`sla_family_metrics`, `chat_family_metrics`) once per organization per
org-local calendar day, then persists whatever they return via
`upsert_daily_metrics`. This module owns the SQL shape; the worker owns
iteration, per-family SAVEPOINT fault isolation, and the commit.

Design decisions this module implements (stage-7-analytics design, D3/D6/D7):

- **D3 — org-local day, not UTC day**: every family builder buckets rows by
  the ORGANIZATION's own IANA timezone (`Organization.timezone`), computed
  in Python via `zoneinfo` (mirroring `app.services.sla.compute_sla_due_at`'s
  existing convention) rather than a Postgres-side `AT TIME ZONE` column
  expression. `local_day_utc_bounds` is the single shared helper every
  family builder calls, so two orgs in different timezones bucket the same
  UTC instant into different `metric_date` rows.
- **D6 — upsert via `ON CONFLICT ... DO UPDATE`**: this is deliberately a
  DIFFERENT conflict strategy from `app.services.sla_event_service.
  record_once`'s `DO NOTHING` (that guards a "never re-notify" idempotency
  invariant). A daily metric is a recomputed FACT, not an audit event — a
  re-run for the same `(organization_id, metric_date, metric_key)` must
  OVERWRITE `metric_value` with the freshly computed one, not silently
  skip. `id` is assigned explicitly (`uuid4()`) per row because
  `UUIDPrimaryKeyMixin`'s Python-side `default=uuid.uuid4` is an ORM-level
  default that does NOT apply to a Core multi-VALUES `insert()`.
- **D7 — `avg_resolution_hours` excludes escalated tickets**: a ticket that
  went through the chatbot-escalation path has a fundamentally different
  resolution-time profile (bot triage time is baked into `created_at`),
  so it is excluded via a `NOT EXISTS` correlated subquery against
  `chat_sessions.escalated_at`, not filtered at the `Ticket` level.

Every metric function that has a natural "undefined" case (dividing by a
zero denominator, averaging zero rows) returns `None` rather than `0` —
the caller (`*_family_metrics`) treats `None` as "omit this metric_key for
this org/day" rather than writing a misleading `0`/`0.0000` row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sla_defaults import SLA_WARNING_ELAPSED_FRACTION
from app.models.category import Category
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.classification import Classification
from app.models.daily_metric import DailyMetric
from app.models.enums import ChatMessageRole, SlaEventType, TicketChannel, TicketStatus
from app.models.message import Message
from app.models.organization import Organization
from app.models.sla_event import SlaEvent
from app.models.ticket import Ticket
from app.models.urgency_level import UrgencyLevel

# `GET /analytics/classifier`'s per-category sample window — only the most
# recent N classifications (by `created_at DESC, id DESC`) per category
# feed the correction-rate computation (see `classifier_review_metrics`).
CLASSIFIER_SAMPLE_WINDOW = 200

# `needs_review` flag threshold — STRICTLY greater than, never `>=`: a
# category at exactly the threshold (e.g. 40/200 = 0.20) is NOT flagged.
# Applied by `app.api.v1.analytics`, not by `classifier_review_metrics`
# itself, keeping the boundary decision in one place.
CLASSIFIER_NEEDS_REVIEW_THRESHOLD = Decimal("0.20")


def local_day_utc_bounds(organization: Organization, local_day: date) -> tuple[datetime, datetime]:
    """Return the `[start, end)` UTC datetime range covering `local_day`'s
    full 00:00-24:00 window in `organization.timezone`.

    Computed via `zoneinfo` on plain Python `datetime`s (same convention as
    `app.services.sla`), NOT a Postgres `AT TIME ZONE` column expression:
    the worker already holds one `Organization` in hand per iteration, so
    there is no need to push the timezone lookup into SQL. Adding
    `timedelta(days=1)` to an aware `zoneinfo`-backed datetime is DST-safe
    here because `zoneinfo` recomputes the UTC offset lazily from the
    (new) wall-clock time on `.astimezone()`, not at addition time.
    """
    tz = ZoneInfo(organization.timezone)
    start_local = datetime.combine(local_day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


# ---------------------------------------------------------------------------
# tickets family — tickets_created
# ---------------------------------------------------------------------------


async def tickets_created_count(
    session: AsyncSession, organization: Organization, local_day: date
) -> int:
    """COUNT of `tickets` rows whose `created_at` falls in `local_day`
    (org-local calendar day)."""
    start, end = local_day_utc_bounds(organization, local_day)
    stmt = select(func.count()).select_from(Ticket).where(
        Ticket.organization_id == organization.id,
        Ticket.created_at >= start,
        Ticket.created_at < end,
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def tickets_family_metrics(
    session: AsyncSession, organization: Organization, local_day: date
) -> list[tuple[str, Decimal]]:
    """The tickets family's sole metric: `tickets_created`. Always written,
    even as `0` — an org with zero tickets created on a day is itself a
    meaningful fact, unlike the rate metrics below."""
    count = await tickets_created_count(session, organization, local_day)
    return [("tickets_created", Decimal(count))]


# ---------------------------------------------------------------------------
# SLA family — tickets_resolved, avg_resolution_hours
# ---------------------------------------------------------------------------


async def tickets_resolved_count(
    session: AsyncSession, organization: Organization, local_day: date
) -> int:
    """COUNT of `sla_events` rows with `event='resolved'` whose
    `occurred_at` falls in `local_day` — event-based, not ticket-based: a
    same-day reopen + re-resolve of the SAME ticket produces two
    `resolved` events (see `sla_event_service.record_resolved`'s module
    docstring) and DELIBERATELY counts twice here."""
    start, end = local_day_utc_bounds(organization, local_day)
    stmt = select(func.count()).select_from(SlaEvent).where(
        SlaEvent.organization_id == organization.id,
        SlaEvent.event == SlaEventType.RESOLVED,
        SlaEvent.occurred_at >= start,
        SlaEvent.occurred_at < end,
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def avg_resolution_hours(
    session: AsyncSession, organization: Organization, local_day: date
) -> Decimal | None:
    """AVG hours between `Ticket.created_at` and each `resolved`
    `SlaEvent.occurred_at` on `local_day`, EXCLUDING any ticket that has at
    least one `chat_sessions` row with `escalated_at IS NOT NULL` (D7).
    Returns `None` when there are zero eligible resolved events that day —
    the caller must not write a misleading `0` row in that case."""
    start, end = local_day_utc_bounds(organization, local_day)
    escalated_exists = (
        select(ChatSession.id)
        .where(
            ChatSession.ticket_id == Ticket.id,
            ChatSession.escalated_at.isnot(None),
        )
        .exists()
    )
    hours_expr = func.extract("epoch", SlaEvent.occurred_at - Ticket.created_at) / 3600.0
    stmt = (
        select(func.avg(hours_expr))
        .select_from(SlaEvent)
        .join(Ticket, Ticket.id == SlaEvent.ticket_id)
        .where(
            SlaEvent.organization_id == organization.id,
            SlaEvent.event == SlaEventType.RESOLVED,
            SlaEvent.occurred_at >= start,
            SlaEvent.occurred_at < end,
            ~escalated_exists,
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def sla_family_metrics(
    session: AsyncSession, organization: Organization, local_day: date
) -> list[tuple[str, Decimal]]:
    """The SLA family's metrics: `tickets_resolved` (always written, even
    as `0`) and `avg_resolution_hours` (omitted entirely when there are no
    eligible resolved events that day — see `avg_resolution_hours`'s
    docstring)."""
    resolved = await tickets_resolved_count(session, organization, local_day)
    metrics: list[tuple[str, Decimal]] = [("tickets_resolved", Decimal(resolved))]
    avg_hours = await avg_resolution_hours(session, organization, local_day)
    if avg_hours is not None:
        metrics.append(("avg_resolution_hours", Decimal(avg_hours)))
    return metrics


# ---------------------------------------------------------------------------
# chat family — chat_sessions_started, chat_escalation_rate, chat_to_ticket_rate
# ---------------------------------------------------------------------------


async def chat_sessions_started_count(
    session: AsyncSession, organization: Organization, local_day: date
) -> int:
    """COUNT of `chat_sessions` rows whose `created_at` (session START)
    falls in `local_day`. This is also the shared denominator for both
    rate metrics below — cohort semantics key entirely on the START day,
    never on when escalation/ticket-linking later happened."""
    start, end = local_day_utc_bounds(organization, local_day)
    stmt = select(func.count()).select_from(ChatSession).where(
        ChatSession.organization_id == organization.id,
        ChatSession.created_at >= start,
        ChatSession.created_at < end,
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def chat_escalation_rate(
    session: AsyncSession, organization: Organization, local_day: date
) -> Decimal | None:
    """Sessions STARTED on `local_day` with `escalated_at IS NOT NULL`,
    divided by all sessions started that day (cohort semantics — the
    escalation itself may happen on a later calendar day; only the START
    day buckets the row). Returns `None` when zero sessions started that
    day (undefined rate), so the caller omits the metric rather than
    writing a misleading `0`."""
    started = await chat_sessions_started_count(session, organization, local_day)
    if started == 0:
        return None
    start, end = local_day_utc_bounds(organization, local_day)
    stmt = select(func.count()).select_from(ChatSession).where(
        ChatSession.organization_id == organization.id,
        ChatSession.created_at >= start,
        ChatSession.created_at < end,
        ChatSession.escalated_at.isnot(None),
    )
    result = await session.execute(stmt)
    escalated = result.scalar_one()
    return Decimal(escalated) / Decimal(started)


async def chat_to_ticket_rate(
    session: AsyncSession, organization: Organization, local_day: date
) -> Decimal | None:
    """Sessions STARTED on `local_day` with `ticket_id IS NOT NULL`,
    divided by all sessions started that day. Same cohort/`None`-on-zero
    contract as `chat_escalation_rate`."""
    started = await chat_sessions_started_count(session, organization, local_day)
    if started == 0:
        return None
    start, end = local_day_utc_bounds(organization, local_day)
    stmt = select(func.count()).select_from(ChatSession).where(
        ChatSession.organization_id == organization.id,
        ChatSession.created_at >= start,
        ChatSession.created_at < end,
        ChatSession.ticket_id.isnot(None),
    )
    result = await session.execute(stmt)
    with_ticket = result.scalar_one()
    return Decimal(with_ticket) / Decimal(started)


async def chat_family_metrics(
    session: AsyncSession, organization: Organization, local_day: date
) -> list[tuple[str, Decimal]]:
    """The chat family's metrics: `chat_sessions_started` (always written,
    even as `0`), plus `chat_escalation_rate`/`chat_to_ticket_rate`
    (each omitted when zero sessions started that day).

    Each rate helper independently re-derives `chat_sessions_started_count`
    rather than accepting it as a parameter — favoring each metric
    function staying independently callable/testable in isolation (as the
    tasks for this PR require) over saving one duplicate COUNT query per
    rollup pass, which is not perf-sensitive for a nightly cron."""
    started = await chat_sessions_started_count(session, organization, local_day)
    metrics: list[tuple[str, Decimal]] = [("chat_sessions_started", Decimal(started))]
    escalation_rate = await chat_escalation_rate(session, organization, local_day)
    if escalation_rate is not None:
        metrics.append(("chat_escalation_rate", escalation_rate))
    to_ticket_rate = await chat_to_ticket_rate(session, organization, local_day)
    if to_ticket_rate is not None:
        metrics.append(("chat_to_ticket_rate", to_ticket_rate))
    return metrics


# ---------------------------------------------------------------------------
# upsert_daily_metrics — INSERT ... ON CONFLICT ... DO UPDATE (D6)
# ---------------------------------------------------------------------------


async def upsert_daily_metrics(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """Upsert `rows` (each a dict with `organization_id`, `metric_date`,
    `metric_key`, `metric_value`) into `daily_metrics`.

    Diverges from `sla_event_service.record_once`'s `DO NOTHING` — the ONLY
    prior `ON CONFLICT` precedent in this codebase — to `DO UPDATE`: a
    daily metric is a recomputed FACT keyed by
    `ux_daily_metrics_org_date_key` (`organization_id`, `metric_date`,
    `metric_key`), so a re-run for the same key must overwrite
    `metric_value` with the freshly computed value, not silently no-op.
    `id` is assigned explicitly per row (`uuid4()`) because
    `UUIDPrimaryKeyMixin`'s Python-side `default=uuid.uuid4` is an ORM
    instance default that does not apply to this Core multi-VALUES
    `insert()`.
    """
    if not rows:
        return
    values = [
        {
            "id": uuid4(),
            "organization_id": row["organization_id"],
            "metric_date": row["metric_date"],
            "metric_key": row["metric_key"],
            "metric_value": row["metric_value"],
        }
        for row in rows
    ]
    stmt = pg_insert(DailyMetric).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["organization_id", "metric_date", "metric_key"],
        set_={
            "metric_value": stmt.excluded.metric_value,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# Overview — LIVE queries (PR4), deliberately NOT read from `daily_metrics`:
# an "open right now" dashboard needs the current instant, not last night's
# rollup snapshot.
# ---------------------------------------------------------------------------


async def open_ticket_counts_by_status(
    session: AsyncSession, organization: Organization
) -> list[tuple[TicketStatus, int]]:
    """COUNT of non-closed `tickets` rows, grouped by `status`. "Open"
    here means `status != 'closed'` — every non-closed status (including
    `escalated`) is its own bucket; `closed` tickets are excluded from
    every overview bucket, the whole point of an "open" dashboard."""
    stmt = (
        select(Ticket.status, func.count())
        .where(
            Ticket.organization_id == organization.id,
            Ticket.status != TicketStatus.CLOSED,
        )
        .group_by(Ticket.status)
    )
    result = await session.execute(stmt)
    return list(result.all())


async def open_ticket_counts_by_urgency(
    session: AsyncSession, organization: Organization
) -> list[tuple[UUID | None, str | None, int]]:
    """COUNT of non-closed `tickets` rows, grouped by `urgency_id` (LEFT
    JOIN `urgency_levels` for the display name). An unclassified open
    ticket (`urgency_id IS NULL`) groups into its own `(None, None, n)`
    row rather than being silently dropped."""
    stmt = (
        select(Ticket.urgency_id, UrgencyLevel.name, func.count())
        .select_from(Ticket)
        .outerjoin(UrgencyLevel, UrgencyLevel.id == Ticket.urgency_id)
        .where(
            Ticket.organization_id == organization.id,
            Ticket.status != TicketStatus.CLOSED,
        )
        .group_by(Ticket.urgency_id, UrgencyLevel.name)
    )
    result = await session.execute(stmt)
    return list(result.all())


async def sla_window_counts(
    session: AsyncSession, organization: Organization, *, now: datetime
) -> tuple[int, int]:
    """`(warning_count, breached_count)` for currently open, classified
    (`sla_due_at IS NOT NULL`) tickets, per `SLA_WARNING_ELAPSED_FRACTION`
    (`app.core.sla_defaults`):

    - **warning**: `now >= created_at + (sla_due_at - created_at) *
      fraction` AND `now < sla_due_at` — the elapsed fraction of the SLA
      window has crossed the threshold, but the due date has not yet
      passed.
    - **breached**: `now >= sla_due_at` — the due date has already
      passed.

    A ticket below the warning threshold is counted in neither bucket.
    `closed` tickets are excluded — an SLA window only matters for a
    still-open ticket. `now` is caller-supplied (not read from the
    system clock here) so this stays a pure, deterministically testable
    query function, matching this module's existing convention of taking
    time as an explicit parameter (see `local_day_utc_bounds`)."""
    elapsed_threshold = Ticket.created_at + (
        (Ticket.sla_due_at - Ticket.created_at) * SLA_WARNING_ELAPSED_FRACTION
    )
    base = (
        select(func.count())
        .select_from(Ticket)
        .where(
            Ticket.organization_id == organization.id,
            Ticket.status != TicketStatus.CLOSED,
            Ticket.sla_due_at.isnot(None),
        )
    )
    warning_stmt = base.where(elapsed_threshold <= now, Ticket.sla_due_at > now)
    breached_stmt = base.where(Ticket.sla_due_at <= now)
    warning = (await session.execute(warning_stmt)).scalar_one()
    breached = (await session.execute(breached_stmt)).scalar_one()
    return warning, breached


async def channel_breakdown_counts(
    session: AsyncSession, organization: Organization, *, since: datetime
) -> list[tuple[TicketChannel, int]]:
    """COUNT of `tickets` rows created on/after `since`, grouped by
    `channel` — backs the overview's rolling lookback-window channel
    breakdown (caller passes `since = now - timedelta(days=30)`)."""
    stmt = (
        select(Ticket.channel, func.count())
        .where(
            Ticket.organization_id == organization.id,
            Ticket.created_at >= since,
        )
        .group_by(Ticket.channel)
    )
    result = await session.execute(stmt)
    return list(result.all())


# ---------------------------------------------------------------------------
# Classifier review — LIVE query (PR4)
# ---------------------------------------------------------------------------


async def classifier_review_metrics(
    session: AsyncSession, organization: Organization
) -> list[tuple[UUID, str, int, Decimal]]:
    """Per-category classifier correction-rate review, over each
    category's most recent `CLASSIFIER_SAMPLE_WINDOW` (200) classifications
    (`ROW_NUMBER() OVER (PARTITION BY tickets.category_id ORDER BY
    classifications.created_at DESC, classifications.id DESC)`), for
    `GET /api/v1/analytics/classifier` (stage 7 — analytics, PR4).

    Returns `(category_id, category_name, sample_size, correction_rate)`
    tuples, where `correction_rate = AVG(human_corrected::int)` over the
    windowed sample (`sample_size` is that window's actual row count, up
    to `CLASSIFIER_SAMPLE_WINDOW`, never more). The caller (API layer)
    flags `needs_review` (`correction_rate > CLASSIFIER_NEEDS_REVIEW_
    THRESHOLD`, STRICTLY greater than — exactly 40/200 = 0.20 is NOT
    flagged) rather than this function, keeping the boundary decision in
    one place.

    A ticket with `category_id IS NULL` (not yet classified into a
    taxonomy category) is excluded entirely — "per-category" review has
    no meaning for an uncategorized ticket.
    """
    ranked = (
        select(
            Ticket.category_id.label("category_id"),
            Classification.human_corrected.label("human_corrected"),
            func.row_number()
            .over(
                partition_by=Ticket.category_id,
                order_by=(Classification.created_at.desc(), Classification.id.desc()),
            )
            .label("rn"),
        )
        .select_from(Classification)
        .join(Ticket, Ticket.id == Classification.ticket_id)
        .where(
            Ticket.organization_id == organization.id,
            Ticket.category_id.isnot(None),
        )
        .subquery()
    )
    stmt = (
        select(
            ranked.c.category_id,
            Category.name,
            func.count().label("sample_size"),
            func.avg(cast(ranked.c.human_corrected, Integer)).label("correction_rate"),
        )
        .select_from(ranked)
        .join(Category, Category.id == ranked.c.category_id)
        .where(ranked.c.rn <= CLASSIFIER_SAMPLE_WINDOW)
        .group_by(ranked.c.category_id, Category.name)
    )
    result = await session.execute(stmt)
    return [
        (category_id, category_name, sample_size, Decimal(correction_rate))
        for category_id, category_name, sample_size, correction_rate in result.all()
    ]


# ---------------------------------------------------------------------------
# RAG suggestion usage — LIVE query (PR4)
# ---------------------------------------------------------------------------


async def rag_suggestion_totals(
    session: AsyncSession, organization: Organization
) -> tuple[int, int]:
    """`(suggestions_generated, suggestions_used)` from `messages` joined
    to `tickets` for org scope: `suggestions_generated = COUNT(*) FILTER
    (WHERE messages.is_ai_suggestion)`, `suggestions_used = COUNT(DISTINCT
    messages.based_on_suggestion_id)` (a `COUNT(DISTINCT ...)` already
    excludes NULLs, so no explicit `FILTER` is needed there). Source is
    `messages` (ticket-thread messages, RAG-suggested replies) — NEVER
    `chat_messages` (chatbot conversation turns), a different table
    entirely with no `is_ai_suggestion`/`based_on_suggestion_id` columns
    at all."""
    generated_expr = func.count().filter(Message.is_ai_suggestion.is_(True))
    used_expr = func.count(func.distinct(Message.based_on_suggestion_id))
    stmt = (
        select(generated_expr, used_expr)
        .select_from(Message)
        .join(Ticket, Ticket.id == Message.ticket_id)
        .where(Ticket.organization_id == organization.id)
    )
    generated, used = (await session.execute(stmt)).one()
    return generated, used


async def rag_category_breakdown(
    session: AsyncSession, organization: Organization
) -> list[tuple[UUID, str, int, int]]:
    """Per-category `(category_id, category_name, generated, used)` RAG
    suggestion breakdown, via `tickets.category_id`. A ticket with no
    category (`category_id IS NULL`) is excluded, same rationale as
    `classifier_review_metrics`."""
    generated_expr = func.count().filter(Message.is_ai_suggestion.is_(True))
    used_expr = func.count(func.distinct(Message.based_on_suggestion_id))
    stmt = (
        select(Ticket.category_id, Category.name, generated_expr, used_expr)
        .select_from(Message)
        .join(Ticket, Ticket.id == Message.ticket_id)
        .join(Category, Category.id == Ticket.category_id)
        .where(
            Ticket.organization_id == organization.id,
            Ticket.category_id.isnot(None),
        )
        .group_by(Ticket.category_id, Category.name)
    )
    result = await session.execute(stmt)
    return list(result.all())


# ---------------------------------------------------------------------------
# Chatbot — LIVE queries (PR4)
# ---------------------------------------------------------------------------


async def chatbot_session_totals(
    session: AsyncSession, organization: Organization
) -> tuple[int, int]:
    """`(total_sessions, escalated_sessions)` — ALL-TIME totals, live
    queried directly against `chat_sessions` rather than reused from
    `chat_family_metrics` (PR3): that function computes a single
    ORG-LOCAL CALENDAR DAY's cohort, not an all-time total, so it does
    not fit this endpoint's "totals to date" shape."""
    total_stmt = (
        select(func.count())
        .select_from(ChatSession)
        .where(ChatSession.organization_id == organization.id)
    )
    escalated_stmt = (
        select(func.count())
        .select_from(ChatSession)
        .where(
            ChatSession.organization_id == organization.id,
            ChatSession.escalated_at.isnot(None),
        )
    )
    total = (await session.execute(total_stmt)).scalar_one()
    escalated = (await session.execute(escalated_stmt)).scalar_one()
    return total, escalated


async def chatbot_avg_messages_per_session(
    session: AsyncSession, organization: Organization
) -> Decimal | None:
    """`total chat_messages / total chat_sessions` for the organization —
    `None` when there are zero sessions (undefined average), matching
    this module's `None`-on-zero-denominator convention. Deliberately
    divides by ALL sessions (not just sessions with >=1 message) so a
    session that escalated with zero bot turns still counts toward the
    denominator."""
    total_sessions_stmt = (
        select(func.count())
        .select_from(ChatSession)
        .where(ChatSession.organization_id == organization.id)
    )
    total_sessions = (await session.execute(total_sessions_stmt)).scalar_one()
    if total_sessions == 0:
        return None
    total_messages_stmt = (
        select(func.count())
        .select_from(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(ChatSession.organization_id == organization.id)
    )
    total_messages = (await session.execute(total_messages_stmt)).scalar_one()
    return Decimal(total_messages) / Decimal(total_sessions)


async def chatbot_tool_usage_counts(
    session: AsyncSession, organization: Organization
) -> list[tuple[str, int]]:
    """`(tool_name, count)` for every `chat_messages` row with
    `role='tool'`, grouped by `tool_name` — `tool_name` is `NOT NULL`
    whenever `role='tool'` (`ck_chat_messages_tool_name_iff_role_tool`),
    so no `None` bucket is possible here."""
    stmt = (
        select(ChatMessage.tool_name, func.count())
        .select_from(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.chat_session_id)
        .where(
            ChatSession.organization_id == organization.id,
            ChatMessage.role == ChatMessageRole.TOOL,
        )
        .group_by(ChatMessage.tool_name)
    )
    result = await session.execute(stmt)
    return list(result.all())
