"""Unit tests for `app.services.channel_identity` — inbound-message sender
identity resolution + auto-provisioning for email and WhatsApp (stage 5 —
multichannel).

`AsyncSession` is mocked, following the pattern established by
`tests/unit/test_organization_service.py` / `tests/unit/test_message_
service.py` — the lookup query itself is a plain unscoped `select(User)`
(see `app.services.channel_identity`'s module docstring for why this is
correct, not a bypass), so no real Postgres connection is required to
prove the resolution/auto-provisioning/cross-org-conflict control flow.

Isolation invariant I4: a sender whose email/phone already belongs to a
user in a DIFFERENT organization must be silently rejected — no ticket,
no user, ever created for that request.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.enums import TicketChannel, UserRole, UserStatus
from app.models.user import User
from app.services.channel_identity import (
    CrossOrgSenderConflict,
    normalize_msisdn,
    resolve_email_sender_identity,
    resolve_whatsapp_sender_identity,
)


def _session_returning(scalar_result) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session.execute.return_value = execute_result
    session.add = MagicMock()
    return session


# ---------------------------------------------------------------------------
# resolve_email_sender_identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_email_identity_returns_existing_same_org_user() -> None:
    org_id = uuid4()
    existing = User(
        id=uuid4(),
        organization_id=org_id,
        name="Jane Tenant",
        email="jane@example.com",
        role=UserRole.TENANT,
        status=UserStatus.ACTIVE,
        password_hash="hashed",
    )
    session = _session_returning(existing)

    result = await resolve_email_sender_identity(
        session, organization_id=org_id, email="jane@example.com"
    )

    assert result is existing
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_email_identity_lookup_is_case_insensitive() -> None:
    """The lookup itself must be case-insensitive — asserted on the
    compiled SQL, since the mocked session can't prove matching by value."""
    org_id = uuid4()
    session = _session_returning(None)
    session.flush = AsyncMock()

    await resolve_email_sender_identity(
        session, organization_id=org_id, email="Jane@Example.com"
    )

    compiled_stmt = str(session.execute.call_args.args[0])
    assert "lower(" in compiled_stmt.lower()


@pytest.mark.asyncio
async def test_resolve_email_identity_auto_provisions_a_pending_tenant_when_not_found() -> None:
    org_id = uuid4()
    session = _session_returning(None)
    session.flush = AsyncMock()

    result = await resolve_email_sender_identity(
        session, organization_id=org_id, email="new-tenant@example.com", display_name="New Tenant"
    )

    session.add.assert_called_once_with(result)
    assert result.organization_id == org_id
    assert result.email == "new-tenant@example.com"
    assert result.role == UserRole.TENANT
    assert result.status == UserStatus.PENDING
    assert result.password_hash is None
    assert result.auto_provisioned_channel == TicketChannel.EMAIL
    assert result.name == "New Tenant"


@pytest.mark.asyncio
async def test_resolve_email_identity_auto_provisioned_name_falls_back_to_address() -> None:
    org_id = uuid4()
    session = _session_returning(None)
    session.flush = AsyncMock()

    result = await resolve_email_sender_identity(
        session, organization_id=org_id, email="new-tenant@example.com"
    )

    assert result.name == "new-tenant@example.com"


@pytest.mark.asyncio
async def test_resolve_email_identity_cross_org_conflict_creates_nothing() -> None:
    """Isolation invariant I4: a sender that already belongs to a
    DIFFERENT organization must be rejected, and no user is ever
    created/staged for the request."""
    other_org_id = uuid4()
    requesting_org_id = uuid4()
    existing = User(
        id=uuid4(),
        organization_id=other_org_id,
        name="Someone Else",
        email="shared@example.com",
        role=UserRole.TENANT,
        status=UserStatus.ACTIVE,
        password_hash="hashed",
    )
    session = _session_returning(existing)

    with pytest.raises(CrossOrgSenderConflict) as exc_info:
        await resolve_email_sender_identity(
            session, organization_id=requesting_org_id, email="shared@example.com"
        )

    assert exc_info.value.organization_id == requesting_org_id
    assert exc_info.value.existing_user_organization_id == other_org_id
    assert exc_info.value.channel == TicketChannel.EMAIL
    session.add.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_whatsapp_sender_identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_whatsapp_identity_returns_existing_same_org_user() -> None:
    org_id = uuid4()
    existing = User(
        id=uuid4(),
        organization_id=org_id,
        name="Jane Tenant",
        email="wa+15551234567@whatsapp.invalid",
        phone_number="+15551234567",
        role=UserRole.TENANT,
        status=UserStatus.PENDING,
        password_hash=None,
    )
    session = _session_returning(existing)

    result = await resolve_whatsapp_sender_identity(
        session, organization_id=org_id, wa_id="15551234567"
    )

    assert result is existing
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_whatsapp_identity_auto_provisions_a_pending_tenant_when_not_found() -> None:
    org_id = uuid4()
    session = _session_returning(None)
    session.flush = AsyncMock()

    result = await resolve_whatsapp_sender_identity(
        session, organization_id=org_id, wa_id="15551234567", display_name="Jane Tenant"
    )

    session.add.assert_called_once_with(result)
    assert result.organization_id == org_id
    assert result.email == "wa+15551234567@whatsapp.invalid"
    assert result.phone_number == "+15551234567"
    assert result.role == UserRole.TENANT
    assert result.status == UserStatus.PENDING
    assert result.password_hash is None
    assert result.auto_provisioned_channel == TicketChannel.WHATSAPP
    assert result.name == "Jane Tenant"


@pytest.mark.asyncio
async def test_resolve_whatsapp_identity_auto_provisioned_name_falls_back_to_wa_id() -> None:
    org_id = uuid4()
    session = _session_returning(None)
    session.flush = AsyncMock()

    result = await resolve_whatsapp_sender_identity(
        session, organization_id=org_id, wa_id="15551234567"
    )

    assert result.name == "15551234567"


@pytest.mark.asyncio
async def test_resolve_whatsapp_identity_cross_org_conflict_creates_nothing() -> None:
    other_org_id = uuid4()
    requesting_org_id = uuid4()
    existing = User(
        id=uuid4(),
        organization_id=other_org_id,
        name="Someone Else",
        email="wa+15551234567@whatsapp.invalid",
        phone_number="+15551234567",
        role=UserRole.TENANT,
        status=UserStatus.PENDING,
        password_hash=None,
    )
    session = _session_returning(existing)

    with pytest.raises(CrossOrgSenderConflict) as exc_info:
        await resolve_whatsapp_sender_identity(
            session, organization_id=requesting_org_id, wa_id="15551234567"
        )

    assert exc_info.value.channel == TicketChannel.WHATSAPP
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_whatsapp_identity_rejects_a_duplicate_phone_number_via_flush() -> None:
    """A race between two concurrent auto-provisioning attempts for the
    same `wa_id` is closed by the real `ix_users_phone_number_unique`
    constraint (migration 0007) — proven here at the propagation level
    (the service must NOT swallow the `IntegrityError`); the actual
    Postgres-constraint enforcement itself needs a live database (see
    `tests/integration/test_channel_identity_constraints.py`)."""
    org_id = uuid4()
    session = _session_returning(None)
    session.flush = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup")))

    with pytest.raises(IntegrityError):
        await resolve_whatsapp_sender_identity(session, organization_id=org_id, wa_id="15551234567")


# ---------------------------------------------------------------------------
# normalize_msisdn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("15551234567", "+15551234567"),
        ("+15551234567", "+15551234567"),
        ("1 555 123 4567", "+15551234567"),
        ("1-555-123-4567", "+15551234567"),
        ("(555) 123-4567-891", "+5551234567891"),
    ],
)
def test_normalize_msisdn_accepts_various_raw_forms(raw: str, expected: str) -> None:
    assert normalize_msisdn(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abc",
        "0123456789",  # leading zero after country code stripped -> invalid
        "+1",  # too short
        "+" + "1" * 20,  # too long
    ],
)
def test_normalize_msisdn_rejects_unnormalizable_input(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_msisdn(raw)
