"""Unit tests for `app.schemas.organization` — stage 5 (multichannel)
additions: WhatsApp/email channel configuration on `OrganizationUpdate`/
`OrganizationResponse`.

Isolation invariant I3: no GET/PATCH `/organizations/:id` response may ever
contain the WhatsApp access token, plaintext or ciphertext. Asserted here
against the RAW serialized JSON bytes `OrganizationResponse` produces (the
exact bytes FastAPI's `response_model` serialization would send over the
wire), not just the schema's declared field list — a field could be
declared `exclude=True` and still leak if a test only checked
`model_fields`, so the proof has to be on the actual output.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.organization import Organization
from app.schemas.organization import OrganizationCreate, OrganizationResponse, OrganizationUpdate


def _make_org(**overrides) -> Organization:
    org = Organization(id=uuid4(), name="Acme Corp", chat_widget_key="widget-key-123")
    org.created_at = datetime.now(UTC)
    for key, value in overrides.items():
        setattr(org, key, value)
    return org


def test_organization_update_accepts_whatsapp_and_email_fields() -> None:
    update = OrganizationUpdate(
        whatsapp_phone_number_id="1234567890",
        whatsapp_access_token="super-secret-token",
        support_email_address="support@acme.example",
    )

    assert update.whatsapp_phone_number_id == "1234567890"
    assert update.whatsapp_access_token.get_secret_value() == "super-secret-token"
    assert update.support_email_address == "support@acme.example"


def test_organization_update_rejects_an_invalid_support_email_address() -> None:
    with pytest.raises(ValidationError):
        OrganizationUpdate(support_email_address="not-an-email")


def test_organization_update_secret_str_never_appears_in_repr() -> None:
    """`SecretStr` masks the value in `repr()`/`str()` by design — this
    proves the schema actually uses `SecretStr` and not a plain `str`."""
    update = OrganizationUpdate(whatsapp_access_token="super-secret-token")

    assert "super-secret-token" not in repr(update)
    assert "super-secret-token" not in str(update)


def test_organization_update_model_dump_never_emits_the_encrypted_column_name() -> None:
    """The deliberate field-name mismatch (`whatsapp_access_token`, never
    `whatsapp_access_token_encrypted`) that keeps plaintext from
    structurally reaching the encrypted column through the generic
    `**fields` -> `setattr` update pipe — see
    `organization_service.update_organization`."""
    update = OrganizationUpdate(whatsapp_access_token="super-secret-token")

    dumped = update.model_dump(exclude_unset=True)

    assert "whatsapp_access_token" in dumped
    assert "whatsapp_access_token_encrypted" not in dumped


def test_organization_response_never_includes_the_token_when_configured() -> None:
    org = _make_org(
        whatsapp_phone_number_id="1234567890",
        whatsapp_access_token_encrypted="gAAAAA-fake-ciphertext-blob",
        support_email_address="support@acme.example",
    )

    raw_json = OrganizationResponse.model_validate(org).model_dump_json()

    assert "gAAAAA-fake-ciphertext-blob" not in raw_json
    assert '"whatsapp_access_token_encrypted"' not in raw_json
    # The `_set` flag key legitimately contains "whatsapp_access_token" as
    # a substring — check for the token FIELD KEY specifically, not a
    # loose substring match, so this assertion cannot pass by accident.
    assert '"whatsapp_access_token":' not in raw_json
    assert '"whatsapp_access_token_set":true' in raw_json


def test_organization_response_reports_token_not_set_when_absent() -> None:
    org = _make_org()

    raw_json = OrganizationResponse.model_validate(org).model_dump_json()

    assert '"whatsapp_access_token_set":false' in raw_json


def test_organization_response_exposes_whatsapp_phone_number_id_and_support_email() -> None:
    org = _make_org(
        whatsapp_phone_number_id="1234567890",
        whatsapp_access_token_encrypted="gAAAAA-fake-ciphertext-blob",
        support_email_address="support@acme.example",
    )

    response = OrganizationResponse.model_validate(org)

    assert response.whatsapp_phone_number_id == "1234567890"
    assert response.support_email_address == "support@acme.example"


# ---------------------------------------------------------------------------
# Stage 6 — queues + SLA (PR7a): timezone/business_hours validators
# ---------------------------------------------------------------------------


def test_organization_create_defaults_timezone_and_business_hours_to_none() -> None:
    """`None` on `OrganizationCreate` means "let the service apply the
    platform default" — see `organization_service.create_organization`."""
    create = OrganizationCreate(name="Acme Corp")

    assert create.timezone is None
    assert create.business_hours is None


def test_organization_create_accepts_a_valid_iana_timezone() -> None:
    create = OrganizationCreate(name="Acme Corp", timezone="Europe/Madrid")

    assert create.timezone == "Europe/Madrid"


def test_organization_create_rejects_an_invalid_iana_timezone() -> None:
    with pytest.raises(ValidationError):
        OrganizationCreate(name="Acme Corp", timezone="Not/A/Zone")


def test_organization_create_accepts_valid_business_hours() -> None:
    hours = {"mon": [["09:00", "18:00"]]}
    create = OrganizationCreate(name="Acme Corp", business_hours=hours)

    assert create.business_hours == hours


def test_organization_create_rejects_malformed_business_hours() -> None:
    """Proves the schema validator delegates to
    `app.services.sla.parse_business_hours` rather than reimplementing its
    rules — see `tests/unit/test_sla.py`'s `parse_business_hours` section
    for the exhaustive set of rejected shapes; not duplicated here."""
    with pytest.raises(ValidationError):
        OrganizationCreate(name="Acme Corp", business_hours={"monday": [["09:00", "18:00"]]})


def test_organization_update_accepts_a_valid_iana_timezone() -> None:
    update = OrganizationUpdate(timezone="Europe/Madrid")

    assert update.timezone == "Europe/Madrid"


def test_organization_update_rejects_an_invalid_iana_timezone() -> None:
    with pytest.raises(ValidationError):
        OrganizationUpdate(timezone="Not/A/Zone")


def test_organization_update_accepts_valid_business_hours() -> None:
    hours = {"mon": [["09:00", "18:00"]]}
    update = OrganizationUpdate(business_hours=hours)

    assert update.business_hours == hours


def test_organization_update_rejects_malformed_business_hours() -> None:
    with pytest.raises(ValidationError):
        OrganizationUpdate(business_hours={"monday": [["09:00", "18:00"]]})
