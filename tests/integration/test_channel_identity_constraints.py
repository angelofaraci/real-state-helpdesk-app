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

import pytest


@pytest.mark.skip(
    reason="Requires a live Postgres instance (see docker-compose.yml); "
    "not reachable in CI (no Postgres service configured). Run manually "
    "with `docker compose up -d db && pytest tests/integration -m ''`."
)
async def test_concurrent_whatsapp_auto_provisioning_for_the_same_wa_id_is_rejected() -> None:
    """Two concurrent `resolve_whatsapp_sender_identity` calls for the
    same never-before-seen `wa_id` (a race the webhook route cannot fully
    prevent at the application level) must have exactly one succeed and
    the other raise `IntegrityError` against the real
    `ix_users_phone_number_unique` index — never two `User` rows for one
    WhatsApp sender."""


@pytest.mark.skip(
    reason="Requires a live Postgres instance (see docker-compose.yml); "
    "not reachable in CI (no Postgres service configured). Run manually "
    "with `docker compose up -d db && pytest tests/integration -m ''`."
)
async def test_email_lookup_matches_case_insensitively_against_real_postgres() -> None:
    """`resolve_email_sender_identity` must find an existing
    `Jane@Example.com` user when the inbound webhook delivers
    `jane@example.com` (or any other casing), against the real
    `ix_users_email_lower_unique` functional index."""
