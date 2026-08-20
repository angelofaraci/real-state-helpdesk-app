"""Tests for `app.workers.whatsapp.send_whatsapp_reply` — the async worker
job that delivers (or defers) a WhatsApp reply already computed by
`process_whatsapp_message` (stage 5 — multichannel, PR5 — WhatsApp worker +
outbound; design ADR-16).

`AsyncSession` is mocked; `whatsapp_client.send_text` is monkeypatched at
the module boundary — this module owns proving the JOB's own control flow
(24h-window pre-check, Meta error 131047 post-check, deferred-reply
bookkeeping, retry/permanent-failure classification), not re-proving
`whatsapp_client.send_text`'s own internals (already covered by
`tests/unit/test_whatsapp_client.py`).

Central behavior under test: BOTH a 24h-window pre-check miss AND a
post-send Meta error 131047 must append to
`channel_metadata["deferred_replies"]` (capped at 20, logged at WARNING
with ids/reason only — never the reply content) and must NEVER actually
send or silently drop the reply.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import arq
import pytest

from app.core.config import get_settings
from app.models.chat_session import ChatSession
from app.models.enums import ChatSessionStatus, TicketChannel
from app.models.organization import Organization
from app.repositories.organization_repository import OrganizationRepository
from app.services import whatsapp_client
from app.services.whatsapp_client import WhatsAppSendError
from app.workers import whatsapp as worker


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


def _execute_result(*, scalar: object = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    return result


def _session_returning(*results: MagicMock) -> AsyncMock:
    session = AsyncMock()
    session.execute.side_effect = list(results)
    return session


def _org(**overrides) -> Organization:
    defaults = dict(
        id=uuid4(),
        name="Landlord Co",
        whatsapp_phone_number_id="1234567890",
        whatsapp_access_token_encrypted="gAAAAA-fake-ciphertext",
    )
    defaults.update(overrides)
    return Organization(**defaults)


def _chat_session(*, organization_id, last_inbound_at, **overrides) -> ChatSession:
    defaults = dict(
        id=uuid4(),
        organization_id=organization_id,
        user_id=uuid4(),
        ticket_id=None,
        status=ChatSessionStatus.ACTIVE,
        low_confidence_streak=0,
        last_activity_at=datetime.now(UTC),
        channel=TicketChannel.WHATSAPP,
        channel_metadata={
            "wa_id": "15550001111",
            "phone_number_id": "1234567890",
            "profile_name": "Jane",
            "last_inbound_at": last_inbound_at,
            "processed_message_ids": ["wamid.NEW"],
            "deferred_replies": [],
        },
    )
    defaults.update(overrides)
    return ChatSession(**defaults)


def _patch_org(monkeypatch, organization: Organization) -> None:
    monkeypatch.setattr(OrganizationRepository, "get_or_404", AsyncMock(return_value=organization))


# ---------------------------------------------------------------------------
# Success path: within the 24h window, send succeeds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_path_sends_within_window(monkeypatch) -> None:
    org = _org()
    chat_session = _chat_session(
        organization_id=org.id,
        last_inbound_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    session = _session_returning(_execute_result(scalar=chat_session))
    _patch_org(monkeypatch, org)
    fake_send_text = AsyncMock(return_value={"messages": [{"id": "wamid.OUT"}]})
    monkeypatch.setattr(whatsapp_client, "send_text", fake_send_text)

    await worker.send_whatsapp_reply(
        {"session_factory": _session_factory(session)}, chat_session.id, "We'll take a look!"
    )

    fake_send_text.assert_awaited_once_with(
        phone_number_id="1234567890",
        to="15550001111",
        body="We'll take a look!",
        access_token_encrypted="gAAAAA-fake-ciphertext",
    )
    assert chat_session.channel_metadata["deferred_replies"] == []
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 24h-window pre-check miss -> deferred, never sent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_miss_pre_check_defers_without_sending(monkeypatch) -> None:
    org = _org()
    window_hours = get_settings().whatsapp_customer_window_hours
    chat_session = _chat_session(
        organization_id=org.id,
        last_inbound_at=(datetime.now(UTC) - timedelta(hours=window_hours + 1)).isoformat(),
    )
    session = _session_returning(_execute_result(scalar=chat_session))
    _patch_org(monkeypatch, org)
    fake_send_text = AsyncMock()
    monkeypatch.setattr(whatsapp_client, "send_text", fake_send_text)

    await worker.send_whatsapp_reply(
        {"session_factory": _session_factory(session)}, chat_session.id, "We'll take a look!"
    )

    fake_send_text.assert_not_awaited()
    deferred = chat_session.channel_metadata["deferred_replies"]
    assert len(deferred) == 1
    assert deferred[0]["reason"] == "outside_24h_window"
    assert deferred[0]["content"] == "We'll take a look!"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_window_miss_never_logs_reply_content(monkeypatch, caplog) -> None:
    org = _org()
    window_hours = get_settings().whatsapp_customer_window_hours
    chat_session = _chat_session(
        organization_id=org.id,
        last_inbound_at=(datetime.now(UTC) - timedelta(hours=window_hours + 1)).isoformat(),
    )
    session = _session_returning(_execute_result(scalar=chat_session))
    _patch_org(monkeypatch, org)
    monkeypatch.setattr(whatsapp_client, "send_text", AsyncMock())
    secret_content = "the visitor's private issue description, never in a log line"

    with caplog.at_level("WARNING"):
        await worker.send_whatsapp_reply(
            {"session_factory": _session_factory(session)}, chat_session.id, secret_content
        )

    for record in caplog.records:
        assert secret_content not in record.getMessage()


# ---------------------------------------------------------------------------
# Meta error 131047 post-check -> deferred, same as a pre-check miss.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_error_131047_post_check_defers(monkeypatch) -> None:
    org = _org()
    chat_session = _chat_session(
        organization_id=org.id,
        last_inbound_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    session = _session_returning(_execute_result(scalar=chat_session))
    _patch_org(monkeypatch, org)
    monkeypatch.setattr(
        whatsapp_client,
        "send_text",
        AsyncMock(side_effect=WhatsAppSendError(status_code=400, meta_error_code=131047)),
    )

    await worker.send_whatsapp_reply(
        {"session_factory": _session_factory(session)}, chat_session.id, "We'll take a look!"
    )

    deferred = chat_session.channel_metadata["deferred_replies"]
    assert len(deferred) == 1
    assert deferred[0]["reason"] == "outside_24h_window"
    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Deferred replies list is capped at 20 (most-recent-kept).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deferred_replies_list_is_capped_at_twenty(monkeypatch) -> None:
    org = _org()
    window_hours = get_settings().whatsapp_customer_window_hours
    existing_deferrals = [
        {"deferred_at": "2024-01-01T00:00:00+00:00", "reason": "outside_24h_window", "content": f"old-{i}"}
        for i in range(20)
    ]
    chat_session = _chat_session(
        organization_id=org.id,
        last_inbound_at=(datetime.now(UTC) - timedelta(hours=window_hours + 1)).isoformat(),
        channel_metadata={
            "wa_id": "15550001111",
            "phone_number_id": "1234567890",
            "profile_name": "Jane",
            "last_inbound_at": (
                datetime.now(UTC) - timedelta(hours=window_hours + 1)
            ).isoformat(),
            "processed_message_ids": ["wamid.NEW"],
            "deferred_replies": existing_deferrals,
        },
    )
    session = _session_returning(_execute_result(scalar=chat_session))
    _patch_org(monkeypatch, org)
    monkeypatch.setattr(whatsapp_client, "send_text", AsyncMock())

    await worker.send_whatsapp_reply(
        {"session_factory": _session_factory(session)}, chat_session.id, "newest reply"
    )

    deferred = chat_session.channel_metadata["deferred_replies"]
    assert len(deferred) == 20
    assert deferred[-1]["content"] == "newest reply"
    assert deferred[0]["content"] == "old-1"  # old-0 fell off the cap.


# ---------------------------------------------------------------------------
# Other transient/permanent send failures.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_send_error_raises_arq_retry_with_backoff(monkeypatch) -> None:
    org = _org()
    chat_session = _chat_session(
        organization_id=org.id,
        last_inbound_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    session = _session_returning(_execute_result(scalar=chat_session))
    _patch_org(monkeypatch, org)
    monkeypatch.setattr(
        whatsapp_client,
        "send_text",
        AsyncMock(side_effect=WhatsAppSendError(status_code=503, meta_error_code=None)),
    )

    with pytest.raises(arq.Retry) as exc_info:
        await worker.send_whatsapp_reply(
            {"session_factory": _session_factory(session), "job_try": 2},
            chat_session.id,
            "hi",
        )

    assert exc_info.value.defer_score == (2**2) * 1000
    session.commit.assert_not_awaited()
    assert chat_session.channel_metadata["deferred_replies"] == []


@pytest.mark.asyncio
async def test_permanent_send_error_is_logged_and_swallowed(monkeypatch) -> None:
    org = _org()
    chat_session = _chat_session(
        organization_id=org.id,
        last_inbound_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    session = _session_returning(_execute_result(scalar=chat_session))
    _patch_org(monkeypatch, org)
    monkeypatch.setattr(
        whatsapp_client,
        "send_text",
        AsyncMock(side_effect=WhatsAppSendError(status_code=401, meta_error_code=None)),
    )

    # Must not raise.
    await worker.send_whatsapp_reply(
        {"session_factory": _session_factory(session)}, chat_session.id, "hi"
    )

    session.commit.assert_not_awaited()
    assert chat_session.channel_metadata["deferred_replies"] == []
