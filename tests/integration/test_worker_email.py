"""Tests for `app.workers.email` — the async outbound-email-reply worker
job (`send_ticket_email_reply`) and its `enqueue_ticket_email_reply` API-
layer hook (stage 5 — multichannel, PR3 — email outbound; design ADR-15).

`AsyncSession` is mocked (no live Postgres in this sandbox); the repository
`get_or_404` calls are monkeypatched directly, following the exact pattern
`tests/unit/test_message_service.py` establishes — this module owns proving
the JOB's own control flow (idempotency, header construction, retry
classification, metadata bookkeeping), not re-proving those already-tested
repository/scope internals.

Central behaviors under test:

- The outbound `Message-ID` is DETERMINISTIC (`<helpdesk-{message.id}@
  {settings.email_message_id_domain}>`), never randomly generated, so a
  duplicate arq delivery of this same job is a safe no-op (checked via
  `channel_metadata.contains` before ever sending) — idempotency invariant.
- Headers (`From`/`Reply-To`/`To`/`Subject`/`In-Reply-To`/`References`) are
  built from `ticket.channel_metadata` and
  `app.services.email_threading.build_reply_headers` (reused verbatim from
  PR2).
- Transient SMTP errors raise `arq.Retry` with the same exponential-backoff
  `defer` convention as `app.workers.classification.classify_ticket`;
  permanent SMTP errors are logged and swallowed (no retry).
- On success, the deterministic Message-ID is appended to
  `ticket.channel_metadata["message_ids"]` and set as
  `last_outbound_message_id`, then committed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import aiosmtplib
import arq
import pytest

from app.core.config import get_settings
from app.models.enums import AuthorType, TicketChannel
from app.models.message import Message
from app.models.organization import Organization
from app.models.ticket import Ticket
from app.repositories.message_repository import MessageRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.ticket_repository import TicketRepository
from app.services import mailer_service
from app.workers import email as worker


class _SessionContextManager:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _session_factory(session: AsyncMock):
    def factory() -> _SessionContextManager:
        return _SessionContextManager(session)

    return factory


def _org(**overrides) -> Organization:
    defaults = dict(
        id=uuid4(), name="Landlord Co", support_email_address="support@landlord.example"
    )
    defaults.update(overrides)
    return Organization(**defaults)


def _ticket(*, organization_id, channel_metadata, **overrides) -> Ticket:
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id,
        user_id=uuid4(),
        title="Leaking faucet",
        channel=TicketChannel.EMAIL,
        channel_metadata=channel_metadata,
    )
    defaults.update(overrides)
    return Ticket(**defaults)


def _message(*, ticket_id, **overrides) -> Message:
    defaults = dict(
        id=uuid4(),
        ticket_id=ticket_id,
        author_type=AuthorType.AGENT,
        content="We're sending someone over this afternoon.",
        is_ai_suggestion=False,
    )
    defaults.update(overrides)
    return Message(**defaults)


def _metadata(**overrides) -> dict:
    defaults = dict(
        provider="mailgun",
        sender_address="jane@example.com",
        subject_normalized="leaking faucet",
        message_ids=["<inbound-1@example.com>"],
        last_outbound_message_id=None,
    )
    defaults.update(overrides)
    return defaults


def _patch_repos(monkeypatch, *, message: Message, ticket: Ticket, organization: Organization) -> None:
    monkeypatch.setattr(MessageRepository, "get_or_404", AsyncMock(return_value=message))
    monkeypatch.setattr(TicketRepository, "get_or_404", AsyncMock(return_value=ticket))
    monkeypatch.setattr(OrganizationRepository, "get_or_404", AsyncMock(return_value=organization))


def _deterministic_message_id(message: Message) -> str:
    settings = get_settings()
    return f"<helpdesk-{message.id}@{settings.email_message_id_domain}>"


# ---------------------------------------------------------------------------
# send_ticket_email_reply — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_path_sends_expected_headers_and_records_message_id(monkeypatch) -> None:
    org = _org()
    ticket = _ticket(organization_id=org.id, channel_metadata=_metadata())
    message = _message(ticket_id=ticket.id)
    _patch_repos(monkeypatch, message=message, ticket=ticket, organization=org)
    fake_send = AsyncMock()
    monkeypatch.setattr(mailer_service, "send_message", fake_send)
    session = AsyncMock()
    ctx = {"session_factory": _session_factory(session)}

    await worker.send_ticket_email_reply(ctx, message.id, ticket.id, org.id)

    fake_send.assert_awaited_once()
    (sent,), _ = fake_send.call_args
    expected_message_id = _deterministic_message_id(message)

    assert sent["From"] == "support@landlord.example"
    assert sent["Reply-To"] == "support@landlord.example"
    assert sent["To"] == "jane@example.com"
    assert sent["Message-ID"] == expected_message_id
    assert sent["In-Reply-To"] == "<inbound-1@example.com>"
    assert "<inbound-1@example.com>" in sent["References"]
    assert "[#" in sent["Subject"]
    assert message.content in sent.get_content()

    assert expected_message_id in ticket.channel_metadata["message_ids"]
    assert ticket.channel_metadata["last_outbound_message_id"] == expected_message_id
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_success_path_falls_back_to_configured_from_address_when_org_has_none(
    monkeypatch,
) -> None:
    org = _org(support_email_address=None)
    ticket = _ticket(organization_id=org.id, channel_metadata=_metadata())
    message = _message(ticket_id=ticket.id)
    _patch_repos(monkeypatch, message=message, ticket=ticket, organization=org)
    fake_send = AsyncMock()
    monkeypatch.setattr(mailer_service, "send_message", fake_send)
    session = AsyncMock()
    ctx = {"session_factory": _session_factory(session)}

    await worker.send_ticket_email_reply(ctx, message.id, ticket.id, org.id)

    (sent,), _ = fake_send.call_args
    assert sent["From"] == get_settings().email_from_fallback_address


@pytest.mark.asyncio
async def test_success_path_uses_last_outbound_message_id_as_in_reply_to_when_set(
    monkeypatch,
) -> None:
    org = _org()
    metadata = _metadata(
        message_ids=["<inbound-1@example.com>", "<helpdesk-prior@helpdesk.local>"],
        last_outbound_message_id="<helpdesk-prior@helpdesk.local>",
    )
    ticket = _ticket(organization_id=org.id, channel_metadata=metadata)
    message = _message(ticket_id=ticket.id)
    _patch_repos(monkeypatch, message=message, ticket=ticket, organization=org)
    fake_send = AsyncMock()
    monkeypatch.setattr(mailer_service, "send_message", fake_send)
    session = AsyncMock()
    ctx = {"session_factory": _session_factory(session)}

    await worker.send_ticket_email_reply(ctx, message.id, ticket.id, org.id)

    (sent,), _ = fake_send.call_args
    assert sent["In-Reply-To"] == "<helpdesk-prior@helpdesk.local>"


# ---------------------------------------------------------------------------
# send_ticket_email_reply — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_sent_message_id_is_a_no_op(monkeypatch) -> None:
    org = _org()
    message = _message(ticket_id=uuid4())
    already_sent_id = _deterministic_message_id(message)
    metadata = _metadata(message_ids=["<inbound-1@example.com>", already_sent_id])
    ticket = _ticket(organization_id=org.id, channel_metadata=metadata, id=message.ticket_id)
    _patch_repos(monkeypatch, message=message, ticket=ticket, organization=org)
    fake_send = AsyncMock()
    monkeypatch.setattr(mailer_service, "send_message", fake_send)
    session = AsyncMock()
    ctx = {"session_factory": _session_factory(session)}

    await worker.send_ticket_email_reply(ctx, message.id, ticket.id, org.id)

    fake_send.assert_not_awaited()
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# send_ticket_email_reply — transient vs permanent SMTP failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_smtp_error_raises_arq_retry_with_backoff(monkeypatch) -> None:
    org = _org()
    ticket = _ticket(organization_id=org.id, channel_metadata=_metadata())
    message = _message(ticket_id=ticket.id)
    _patch_repos(monkeypatch, message=message, ticket=ticket, organization=org)
    monkeypatch.setattr(
        mailer_service,
        "send_message",
        AsyncMock(side_effect=aiosmtplib.SMTPConnectError("connection refused")),
    )
    session = AsyncMock()
    ctx = {"session_factory": _session_factory(session), "job_try": 3}

    with pytest.raises(arq.Retry) as exc_info:
        await worker.send_ticket_email_reply(ctx, message.id, ticket.id, org.id)

    assert exc_info.value.defer_score == (2**3) * 1000
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_permanent_smtp_error_is_logged_and_swallowed(monkeypatch) -> None:
    org = _org()
    ticket = _ticket(organization_id=org.id, channel_metadata=_metadata())
    message = _message(ticket_id=ticket.id)
    _patch_repos(monkeypatch, message=message, ticket=ticket, organization=org)
    monkeypatch.setattr(
        mailer_service,
        "send_message",
        AsyncMock(side_effect=aiosmtplib.SMTPDataError(550, "mailbox unavailable")),
    )
    session = AsyncMock()
    ctx = {"session_factory": _session_factory(session)}

    # Must not raise.
    await worker.send_ticket_email_reply(ctx, message.id, ticket.id, org.id)

    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# enqueue_ticket_email_reply — the API layer's post-reply hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_ticket_email_reply_opens_a_pool_enqueues_and_closes(monkeypatch) -> None:
    message_id = uuid4()
    ticket_id = uuid4()
    organization_id = uuid4()
    fake_redis = AsyncMock()
    fake_create_pool = AsyncMock(return_value=fake_redis)
    monkeypatch.setattr(worker, "create_pool", fake_create_pool)

    await worker.enqueue_ticket_email_reply(message_id, ticket_id, organization_id)

    fake_create_pool.assert_awaited_once()
    fake_redis.enqueue_job.assert_awaited_once_with(
        "send_ticket_email_reply", message_id, ticket_id, organization_id
    )
    fake_redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_ticket_email_reply_closes_the_pool_even_if_enqueue_fails(
    monkeypatch,
) -> None:
    message_id = uuid4()
    ticket_id = uuid4()
    organization_id = uuid4()
    fake_redis = AsyncMock()
    fake_redis.enqueue_job.side_effect = RuntimeError("redis unreachable")
    monkeypatch.setattr(worker, "create_pool", AsyncMock(return_value=fake_redis))

    with pytest.raises(RuntimeError):
        await worker.enqueue_ticket_email_reply(message_id, ticket_id, organization_id)

    fake_redis.aclose.assert_awaited_once()
