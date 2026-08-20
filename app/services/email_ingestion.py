"""Email ingestion — the inbound-email matching ladder (stage 5 —
multichannel, PR2 — email inbound; design ADR-7).

Given an already-verified, already-org-resolved `InboundEmail`
(`app.api.deps_webhooks.verify_and_parse_email_webhook`), decide whether it
is:

- a REPLAY of a provider Message-ID already recorded on some ticket
  (isolation invariant I5 — no-op, nothing written);
- a REPLY to an existing thread, matched (in order) by:
    1. In-Reply-To/References header (`app.services.email_threading.
       extract_reference_ids`, most recent ancestor first — first hit
       wins);
    2. the `[#xxxxxxxx]` subject tag (`email_threading.parse_ticket_tag`)
       — accepted ONLY if it resolves to EXACTLY one in-scope ticket
       (ambiguity falls through to step 3, never guesses);
    3. sender address + normalized subject, most recent open ticket within
       `settings.email_thread_subject_match_days`;
- or, if none of the above match, the start of a NEW `channel=email`
  ticket.

All four candidates are drawn from a single up-front scan of the sender's
in-scope, `channel=email` tickets (one `session.execute`) — the matching
ladder itself runs entirely in Python over that list; only the WINNING
ticket (if any) is re-fetched `for_update=True` before being mutated, so no
lock is ever held while scanning candidates.

Sender resolution (`app.services.channel_identity.resolve_email_sender_
identity`) runs first and is deliberately left UNCAUGHT here: a
`CrossOrgSenderConflict` (isolation invariant I4) must propagate all the
way to the caller (the webhook route), which logs it at WARNING and acks
the webhook as a no-op — this module must never swallow it or write
anything on that path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.scope import OrgScope
from app.models.enums import TicketChannel, TicketStatus
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.repositories.ticket_repository import TicketRepository
from app.services import channel_identity, message_service, ticket_service
from app.services.channel_metadata import contains, with_appended
from app.services.email_provider import InboundEmail
from app.services.email_threading import (
    extract_reference_ids,
    normalize_subject,
    parse_ticket_tag,
)


class EmailIngestionOutcome(str, Enum):
    REPLAYED = "replayed"
    MESSAGE_APPENDED = "message_appended"
    TICKET_CREATED = "ticket_created"


@dataclass(frozen=True, slots=True)
class EmailIngestionResult:
    """The outcome of `ingest_inbound_email`, plus the ticket it acted on
    (already-existing for `REPLAYED`/`MESSAGE_APPENDED`, freshly created
    for `TICKET_CREATED`) — the webhook route reads `ticket.classification_
    status` off this to decide whether to enqueue classification, mirroring
    `app.api.v1.tickets.create_ticket`'s own `BackgroundTasks` hook."""

    outcome: EmailIngestionOutcome
    ticket: Ticket


async def ingest_inbound_email(
    session: AsyncSession, *, organization: Organization, inbound: InboundEmail
) -> EmailIngestionResult:
    """Ingest one verified, org-resolved inbound email under the matching
    ladder described in this module's docstring."""
    sender = await channel_identity.resolve_email_sender_identity(
        session,
        organization_id=organization.id,
        email=inbound.from_address,
        display_name=inbound.from_display_name,
    )
    scope = OrgScope.from_principal(sender)
    repo = TicketRepository(session, scope)

    candidates = await _load_email_candidates(session, repo)

    replay = _find_replay(candidates, inbound.provider_message_id)
    if replay is not None:
        return EmailIngestionResult(outcome=EmailIngestionOutcome.REPLAYED, ticket=replay)

    matched = (
        _match_by_reference(candidates, inbound)
        or _match_by_subject_tag(candidates, inbound)
        or _match_by_sender_subject_fallback(candidates, inbound)
    )

    if matched is not None:
        return await _append_message(session, repo=repo, scope=scope, ticket_id=matched.id, inbound=inbound)

    return await _create_new_ticket(session, scope=scope, inbound=inbound)


async def _load_email_candidates(session: AsyncSession, repo: TicketRepository) -> list[Ticket]:
    stmt = repo.select().where(Ticket.channel == TicketChannel.EMAIL)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _find_replay(candidates: list[Ticket], provider_message_id: str) -> Ticket | None:
    for ticket in candidates:
        if contains(ticket.channel_metadata, "message_ids", provider_message_id):
            return ticket
    return None


def _match_by_reference(candidates: list[Ticket], inbound: InboundEmail) -> Ticket | None:
    reference_ids = extract_reference_ids(inbound.in_reply_to, inbound.references)
    for reference_id in reference_ids:
        for ticket in candidates:
            if contains(ticket.channel_metadata, "message_ids", reference_id):
                return ticket
    return None


def _match_by_subject_tag(candidates: list[Ticket], inbound: InboundEmail) -> Ticket | None:
    tag = parse_ticket_tag(inbound.subject)
    if tag is None:
        return None
    matches = [ticket for ticket in candidates if str(ticket.id).replace("-", "").startswith(tag)]
    if len(matches) == 1:
        return matches[0]
    return None


def _match_by_sender_subject_fallback(
    candidates: list[Ticket], inbound: InboundEmail
) -> Ticket | None:
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=settings.email_thread_subject_match_days)
    sender_address = inbound.from_address.lower()
    subject_normalized = normalize_subject(inbound.subject)

    eligible = [
        ticket
        for ticket in candidates
        if ticket.status != TicketStatus.CLOSED
        and (ticket.channel_metadata or {}).get("sender_address") == sender_address
        and (ticket.channel_metadata or {}).get("subject_normalized") == subject_normalized
        and ticket.created_at is not None
        and ticket.created_at > cutoff
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda ticket: ticket.created_at)


async def _append_message(
    session: AsyncSession,
    *,
    repo: TicketRepository,
    scope: OrgScope,
    ticket_id,
    inbound: InboundEmail,
) -> EmailIngestionResult:
    # Re-fetch the winning ticket `for_update=True` — no lock was held
    # while scanning `candidates` above.
    ticket = await repo.get_or_404(ticket_id, for_update=True)
    ticket.channel_metadata = with_appended(
        ticket.channel_metadata, "message_ids", inbound.provider_message_id
    )
    await message_service.create_message(
        session, scope=scope, ticket_id=ticket.id, content=inbound.text_body
    )
    return EmailIngestionResult(outcome=EmailIngestionOutcome.MESSAGE_APPENDED, ticket=ticket)


async def _create_new_ticket(
    session: AsyncSession, *, scope: OrgScope, inbound: InboundEmail
) -> EmailIngestionResult:
    settings = get_settings()
    ticket = await ticket_service.create_ticket(
        session,
        scope=scope,
        property_id=None,
        contract_id=None,
        title=inbound.subject.strip() or "(no subject)",
        description=inbound.text_body,
        category_id=None,
        urgency_id=None,
        channel=TicketChannel.EMAIL,
    )
    ticket.channel_metadata = {
        "provider": settings.email_webhook_provider,
        "sender_address": inbound.from_address.lower(),
        "subject_normalized": normalize_subject(inbound.subject),
        "message_ids": [inbound.provider_message_id],
        "last_outbound_message_id": None,
    }
    await session.flush()
    return EmailIngestionResult(outcome=EmailIngestionOutcome.TICKET_CREATED, ticket=ticket)
