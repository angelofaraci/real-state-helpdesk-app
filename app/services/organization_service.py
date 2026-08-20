"""Organization CRUD service (super-admin only).

Authorization is enforced at the API layer via `app.api.deps.require_super_admin`,
not here — this module only handles persistence and conflict mapping.
"""

import secrets
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from app.core.scope import OrgScope
from app.models.organization import Organization
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository
from app.services import taxonomy_seed

# Sentinel distinguishing "no `whatsapp_access_token` key at all" (leave
# the stored token untouched) from "`whatsapp_access_token=None` was
# explicitly passed" (clear the stored token) — `fields.pop(key, None)`
# alone could not tell these two cases apart.
_UNSET = object()

# Every field name whose uniqueness is enforced by a DB index/constraint
# that `update_organization` can violate — used only to build a readable
# `ConflictError` message; the exact set an `IntegrityError` came from is
# not otherwise recoverable from `asyncpg`'s error without parsing its
# constraint name, which is deliberately avoided here.
_UNIQUE_CONSTRAINED_FIELDS = ("name", "whatsapp_phone_number_id", "support_email_address")


class ConflictError(Exception):
    """Raised when an organization update/create collides with an
    existing organization on one of its unique fields: `name`
    (case-insensitive, `ix_organizations_name_lower_unique`),
    `whatsapp_phone_number_id`
    (`ix_organizations_whatsapp_phone_number_id_unique`), or
    `support_email_address` (case-insensitive,
    `ix_organizations_support_email_address_lower_unique`)."""


async def create_organization(session: AsyncSession, *, name: str, actor: User) -> Organization:
    """Create a new organization and seed its default taxonomy (6 default
    categories + 4 default urgency levels). Raises `ConflictError` if an
    organization with the same name (case-insensitive) already exists —
    that check happens BEFORE any taxonomy row is staged, since a scope
    can only be built for an org that has actually persisted.

    `actor` MUST be the super-admin principal creating this organization;
    it is used only to build the `OrgScope` that seeds the taxonomy
    (`OrgScope.for_new_organization` never reads `actor.organization_id`).
    """
    repo = OrganizationRepository(session)
    org = repo.add(name=name, chat_widget_key=secrets.token_urlsafe(32))
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"an organization named '{name}' already exists") from exc

    scope = OrgScope.for_new_organization(org.id, actor=actor)
    await taxonomy_seed.seed_default_taxonomy(session, scope=scope)
    await session.flush()

    return org


async def get_organization(session: AsyncSession, *, organization_id: UUID) -> Organization:
    """Fetch a single organization by id. Raises `NotFoundError` if it does
    not exist."""
    repo = OrganizationRepository(session)
    return await repo.get_or_404(organization_id)


async def update_organization(
    session: AsyncSession, *, organization_id: UUID, **fields: object
) -> Organization:
    """Update an organization's fields. Raises `NotFoundError` if it does
    not exist, or `ConflictError` if the update collides with another
    organization on any of its unique fields (name, WhatsApp phone number
    id, or support email address).

    Stage 5 — multichannel: `fields` may carry a `whatsapp_access_token`
    (a `SecretStr`, as `OrganizationUpdate` declares it — see that
    schema's docstring for why its name deliberately does NOT match the
    `whatsapp_access_token_encrypted` DB column). It is translated to the
    encrypted column here, the one explicit place plaintext ever meets
    `app.core.crypto.encrypt_secret`, before ever reaching the generic
    `repo.update` -> `setattr` pipe. `whatsapp_access_token=None`
    (explicitly passed) clears the stored token; the key being absent
    from `fields` entirely leaves it untouched.
    """
    fields = dict(fields)
    token = fields.pop("whatsapp_access_token", _UNSET)
    if token is not _UNSET:
        if token is None:
            fields["whatsapp_access_token_encrypted"] = None
        else:
            secret_value = token.get_secret_value() if isinstance(token, SecretStr) else token
            fields["whatsapp_access_token_encrypted"] = encrypt_secret(secret_value)

    repo = OrganizationRepository(session)
    org = await repo.update(organization_id, **fields)
    try:
        await session.flush()
    except IntegrityError as exc:
        conflicting = {
            key: fields[key]
            for key in _UNIQUE_CONSTRAINED_FIELDS
            if fields.get(key) is not None
        }
        detail = ", ".join(f"{key}={value!r}" for key, value in conflicting.items())
        raise ConflictError(
            f"organization update conflicts with an existing organization ({detail})"
            if detail
            else "organization update conflicts with an existing organization"
        ) from exc
    return org
