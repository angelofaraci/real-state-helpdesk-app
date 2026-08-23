"""Integration tests for Stage 5 (multichannel) real-Postgres-only
behaviors of `app.services.channel_identity`, following the same
skip-stub pattern as `tests/integration/test_auth_flow_pg.py` and
`tests/integration/test_classification_constraints.py`.

Everything in `tests/unit/test_channel_identity.py` mocks the
`AsyncSession` boundary, which is sufficient to prove the resolution /
auto-provisioning / cross-org-conflict control flow (including that a
duplicate-phone-number `IntegrityError` propagates rather than being
swallowed). It does NOT prove that:

- The real `ix_users_phone_number_unique` unique index (migration 0007)
  actually rejects two concurrently auto-provisioned users for the same
  WhatsApp `wa_id` at the database level.
- The real `ck_users_phone_number_e164` CHECK constraint enforces the
  E.164 shape independently of `normalize_msisdn`'s own Python-side
  validation (defense in depth: a future code path that writes
  `User.phone_number` directly, bypassing `channel_identity`, must still
  be rejected by Postgres itself).
- `func.lower(User.email) == email.lower()` actually executes and matches
  correctly against the real `ix_users_email_lower_unique` functional
  index in Postgres (SQLAlchemy's compiled SQL is exercised by the mocked
  unit tests, but never actually run against a real engine there).

No live Postgres is reachable in this sandbox. These tests are
intentionally skipped rather than faking Postgres-specific behavior with
SQLite, and should be run manually or in CI against
`docker compose up -d postgres` once a Postgres integration test harness
exists.
"""

import secrets
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import TicketChannel, UserRole, UserStatus
from app.models.organization import Organization
from app.models.user import User
from app.services import channel_identity


async def _make_org(db_session: AsyncSession) -> Organization:
    org = Organization(
        name=f"Test Org {uuid4()}",
        chat_widget_key=secrets.token_urlsafe(16),
        timezone="UTC",
    )
    db_session.add(org)
    await db_session.flush()
    return org


async def test_concurrent_whatsapp_auto_provisioning_for_the_same_wa_id_is_rejected(
    db_session: AsyncSession,
) -> None:
    """Two concurrent `resolve_whatsapp_sender_identity` calls for the
    same never-before-seen `wa_id` (a race the webhook route cannot fully
    prevent at the application level) must have exactly one succeed and
    the other raise `IntegrityError` against the real
    `ix_users_phone_number_unique` index — never two `User` rows for one
    WhatsApp sender."""
    org = await _make_org(db_session)
    wa_id = f"1555{uuid4().int % 10_000_000:07d}"
    normalized = channel_identity.normalize_msisdn(wa_id)

    # Simulate the race: both concurrent callers run their lookup BEFORE
    # either has inserted anything, so both observe "no existing user yet"
    # — exactly what `resolve_whatsapp_sender_identity` itself would see
    # under a true race.
    existing_a = await channel_identity._find_user_by_phone_number(db_session, normalized)
    existing_b = await channel_identity._find_user_by_phone_number(db_session, normalized)
    assert existing_a is None
    assert existing_b is None

    # First caller wins the race and successfully auto-provisions.
    first_user = await channel_identity.resolve_whatsapp_sender_identity(
        db_session, organization_id=org.id, wa_id=wa_id
    )
    assert first_user.phone_number == normalized

    # Second caller, having also observed no existing user, attempts the
    # exact same insert `_resolve_or_provision` would perform — Postgres
    # itself (not application code) must reject it.
    duplicate_user = User(
        organization_id=org.id,
        name="duplicate concurrent sender",
        email=f"wa+{wa_id}-second@whatsapp.invalid",
        phone_number=normalized,
        role=UserRole.TENANT,
        status=UserStatus.PENDING,
        password_hash=None,
        auto_provisioned_channel=TicketChannel.WHATSAPP,
    )
    # Scope the failing insert to its own SAVEPOINT: on `IntegrityError`,
    # SQLAlchemy's `begin_nested()` context manager rolls back only THIS
    # nested transaction, leaving `first_user`'s already-flushed insert
    # (outside this savepoint) untouched.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(duplicate_user)
            await db_session.flush()

    result = await db_session.execute(
        select(User).where(User.phone_number == normalized)
    )
    users_with_phone = result.scalars().all()
    assert len(users_with_phone) == 1
    assert users_with_phone[0].id == first_user.id


async def test_email_lookup_matches_case_insensitively_against_real_postgres(
    db_session: AsyncSession,
) -> None:
    """`resolve_email_sender_identity` must find an existing
    `Jane@Example.com` user when the inbound webhook delivers
    `jane@example.com` (or any other casing), against the real
    `ix_users_email_lower_unique` functional index."""
    org = await _make_org(db_session)
    created_email = f"Jane.{uuid4()}@Example.com"
    existing_user = User(
        organization_id=org.id,
        name="Jane",
        email=created_email,
        role=UserRole.TENANT,
        status=UserStatus.ACTIVE,
        password_hash=secrets.token_urlsafe(16),
    )
    db_session.add(existing_user)
    await db_session.flush()

    resolved = await channel_identity.resolve_email_sender_identity(
        db_session,
        organization_id=org.id,
        email=created_email.lower(),
    )

    assert resolved.id == existing_user.id
    # No second user was auto-provisioned for the differently-cased address.
    result = await db_session.execute(
        select(User).where(User.organization_id == org.id)
    )
    assert len(result.scalars().all()) == 1
