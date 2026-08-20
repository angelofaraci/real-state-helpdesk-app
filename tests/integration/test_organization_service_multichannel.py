"""Integration tests for `app.services.organization_service.update_organization`
stage-5 (multichannel) behavior: the WhatsApp-access-token encryption
translation, and `ConflictError` generalized beyond a `name` collision.

Everything below is expressed against a mocked `AsyncSession`/
`OrganizationRepository` boundary (same convention as
`tests/unit/test_organization_service.py`) — control flow and the actual
encrypted value that gets set on the update `fields` dict do not require a
real Postgres connection: `app.core.crypto.encrypt_secret` is pure
crypto, not DB I/O. Placed under `tests/integration/` (rather than
`tests/unit/`) because it exercises the FULL service-layer translation
pipeline (schema `SecretStr` -> encrypted column, `IntegrityError` ->
`ConflictError`) end to end, not an isolated unit.

What this file does NOT prove (documented, matching the skip-stub
convention already established by `tests/integration/test_auth_flow_pg.py`
and `tests/integration/test_classification_constraints.py`): that the two
NEW unique constraints this PR's migration 0007 adds
(`ix_organizations_whatsapp_phone_number_id_unique`,
`ix_organizations_support_email_address_lower_unique`) are actually
enforced by a real Postgres instance. No live Postgres is reachable in
this sandbox; that proof is deferred to a real dev/CI environment, same as
every other Postgres-constraint-only test in this suite.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError

from app.core import crypto
from app.models.organization import Organization
from app.services import organization_service
from app.services.organization_service import ConflictError


def _session_returning(scalar_result) -> AsyncMock:
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = scalar_result
    session.execute.return_value = execute_result
    return session


@pytest.fixture(autouse=True)
def _configured_encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {
        "secret_encryption_key": key
    })())


@pytest.mark.asyncio
async def test_update_organization_encrypts_whatsapp_access_token_before_persisting() -> None:
    existing = Organization(id=uuid4(), name="Acme Corp")
    session = _session_returning(existing)

    updated = await organization_service.update_organization(
        session,
        organization_id=existing.id,
        whatsapp_access_token=SecretStr("plain-whatsapp-token"),
    )

    assert updated.whatsapp_access_token_encrypted is not None
    assert updated.whatsapp_access_token_encrypted != "plain-whatsapp-token"
    assert crypto.decrypt_secret(updated.whatsapp_access_token_encrypted) == (
        "plain-whatsapp-token"
    )
    # Never a `whatsapp_access_token` attribute on the ORM instance itself
    # — only the encrypted column exists on the model.
    assert not hasattr(updated, "whatsapp_access_token")


@pytest.mark.asyncio
async def test_update_organization_clears_the_token_when_explicitly_set_to_none() -> None:
    existing = Organization(
        id=uuid4(), name="Acme Corp", whatsapp_access_token_encrypted="old-ciphertext"
    )
    session = _session_returning(existing)

    updated = await organization_service.update_organization(
        session,
        organization_id=existing.id,
        whatsapp_access_token=None,
    )

    assert updated.whatsapp_access_token_encrypted is None


@pytest.mark.asyncio
async def test_update_organization_leaves_token_untouched_when_field_absent() -> None:
    existing = Organization(
        id=uuid4(), name="Acme Corp", whatsapp_access_token_encrypted="unchanged-ciphertext"
    )
    session = _session_returning(existing)

    updated = await organization_service.update_organization(
        session, organization_id=existing.id, name="New Name"
    )

    assert updated.whatsapp_access_token_encrypted == "unchanged-ciphertext"


@pytest.mark.asyncio
async def test_update_organization_still_raises_conflict_on_name_collision() -> None:
    """Regression: the pre-existing name-collision behavior must keep
    working after generalizing the conflict-mapping branch."""
    existing = Organization(id=uuid4(), name="Old Name")
    session = _session_returning(existing)
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    with pytest.raises(ConflictError):
        await organization_service.update_organization(
            session, organization_id=existing.id, name="Acme Corp"
        )


@pytest.mark.asyncio
async def test_update_organization_raises_conflict_on_whatsapp_phone_number_id_collision() -> None:
    """The old handler hardcoded its message to `fields["name"]`, which
    would have rendered a nonsensical `None` for a non-name conflict — the
    generalized handler must raise `ConflictError` cleanly here too."""
    existing = Organization(id=uuid4(), name="Acme Corp")
    session = _session_returning(existing)
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    with pytest.raises(ConflictError):
        await organization_service.update_organization(
            session, organization_id=existing.id, whatsapp_phone_number_id="1234567890"
        )


@pytest.mark.asyncio
async def test_update_organization_raises_conflict_on_support_email_address_collision() -> None:
    existing = Organization(id=uuid4(), name="Acme Corp")
    session = _session_returning(existing)
    session.flush.side_effect = IntegrityError("stmt", {}, Exception("dup"))

    with pytest.raises(ConflictError):
        await organization_service.update_organization(
            session, organization_id=existing.id, support_email_address="dup@example.com"
        )
